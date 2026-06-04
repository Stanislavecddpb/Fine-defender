"""Воркер выгрузки (M1).

Поток на одного селлера:
  start_run -> fetch_rows (WB) -> normalize -> upsert в raw_transactions
  (идемпотентно) -> finish_run.

Падение клиента после ретраев фиксируется как failed-прогон с error_detail
(dead-letter в журнале ingestion_runs) — выгрузка не «молчит» (раздел 9 ТЗ).
Токен расшифровывается из sellers.wb_token_enc только в памяти, в логи не идёт.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from ..config import AppConfig, get_app_config, get_settings
from ..crypto import TokenCipher
from ..repository import RawTxnRecord, Repository, Seller, utcnow
from ..wb.adapter import normalize_row
from ..wb.client import ReportClient, WbApiError

logger = logging.getLogger(__name__)


@dataclass
class IngestionResult:
    seller_id: str
    status: str            # success | partial | failed
    rows_seen: int
    rows_new: int
    rows_skipped_no_key: int
    error_detail: str | None = None


class IngestionWorker:
    def __init__(
        self,
        repo: Repository,
        client: ReportClient,
        *,
        config: AppConfig | None = None,
        cipher: TokenCipher | None = None,
    ) -> None:
        self._repo = repo
        self._client = client
        self._config = config or get_app_config()
        self._cipher = cipher

    def _default_date_from(self) -> date:
        return date.today() - timedelta(days=self._config.ingestion.default_period_days)

    def _token_for(self, seller: Seller) -> str:
        cipher = self._cipher or TokenCipher()
        return cipher.decrypt(seller.wb_token_enc)

    def run_for_seller(self, seller: Seller, date_from: date | None = None) -> IngestionResult:
        date_from = date_from or self._default_date_from()
        run = self._repo.start_run(seller.id, utcnow())
        rows_seen = 0
        rows_skipped = 0
        batch: list[RawTxnRecord] = []
        try:
            token = self._token_for(seller)
            for raw in self._client.fetch_rows(token, date_from):
                rows_seen += 1
                normalized = normalize_row(raw)
                if normalized is None:
                    rows_skipped += 1  # строка без ключа транзакции — нельзя дедуплицировать
                    continue
                batch.append(RawTxnRecord(wb_txn_key=normalized.txn_key, raw_json=raw))
        except WbApiError as exc:
            self._repo.finish_run(
                run, status="failed", rows_ingested=0, finished_at=utcnow(),
                error_detail=str(exc),
            )
            logger.error("Выгрузка упала для seller=%s: %s", seller.id, exc)
            return IngestionResult(
                seller_id=str(seller.id), status="failed", rows_seen=rows_seen,
                rows_new=0, rows_skipped_no_key=rows_skipped, error_detail=str(exc),
            )

        rows_new = self._repo.upsert_raw_transactions(seller.id, batch)
        status = "partial" if rows_skipped else "success"
        self._repo.finish_run(
            run, status=status, rows_ingested=rows_new, finished_at=utcnow(),
        )
        logger.info(
            "Выгрузка seller=%s: видно=%d новых=%d пропущено(без ключа)=%d status=%s",
            seller.id, rows_seen, rows_new, rows_skipped, status,
        )
        return IngestionResult(
            seller_id=str(seller.id), status=status, rows_seen=rows_seen,
            rows_new=rows_new, rows_skipped_no_key=rows_skipped,
        )

    def run_all(self, date_from: date | None = None) -> list[IngestionResult]:
        results = []
        for seller in self._repo.get_active_sellers():
            results.append(self.run_for_seller(seller, date_from))
        return results
