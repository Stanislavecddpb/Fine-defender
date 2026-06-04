"""Классификатор штрафов (M2). Только rule-based, один интересующий тип.

Логика (раздел 6 ТЗ):
1. Из сырых транзакций отбираем штрафы: penalty > 0 ИЛИ тип операции входит в
   множество штрафов (config.classifier.fine_operation_types).
2. По строке-расшифровке причины определяем категорию. На MVP интересует только
   oversize_logistics (повышенная логистика по результатам обмеров); маппинг
   причина->категория — в конфиге (паттерны), без правки кода.
3. Прочие штрафы пишем с category='other' и дальше не обрабатываем (но храним).
4. Для oversize_logistics: dispute_deadline = charged_at + window_days,
   recoverable_est = amount * recoverable_ratio, status='disputable'.

Дедлайн и пороги — из конфига, не хардкод.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .config import AppConfig, get_app_config
from .repository import FineRecord, Repository
from .wb.adapter import NormalizedRow, normalize_row

logger = logging.getLogger(__name__)

OTHER = "other"


@dataclass
class ClassifyResult:
    seller_id: str
    seen: int
    fines_new: int
    by_category: dict[str, int]
    skipped_no_key: int
    skipped_not_fine: int


class Classifier:
    def __init__(self, repo: Repository, *, config: AppConfig | None = None) -> None:
        self._repo = repo
        self._config = config or get_app_config()
        cfg = self._config.classifier
        self._fine_ops = [s.upper() for s in cfg.fine_operation_types]
        # category -> [lowercased patterns]
        self._patterns: dict[str, list[str]] = {
            cat: [p.lower() for p in pats] for cat, pats in cfg.reason_patterns.items()
        }

    def _is_fine(self, row: NormalizedRow) -> bool:
        if row.penalty > 0:
            return True
        op = row.operation_type.upper()
        return any(fo in op for fo in self._fine_ops)

    def _categorize(self, row: NormalizedRow) -> str:
        reason = row.reason_raw.lower()
        for category, patterns in self._patterns.items():
            if any(p in reason for p in patterns):
                return category
        return OTHER

    def _deadline(self, charged_at: date) -> date:
        return charged_at + timedelta(days=self._config.dispute.window_days)

    def _build_fine(self, raw_txn_id: int, row: NormalizedRow) -> FineRecord:
        category = self._categorize(row)
        if category == OTHER:
            # прочие штрафы храним, но не готовим к спору
            return FineRecord(
                raw_txn_id=raw_txn_id, category=OTHER, reason_raw=row.reason_raw,
                amount=row.penalty, charged_at=row.charged_at or date.today(),
                dispute_deadline=None, recoverable_est=None, status="new",
            )
        charged = row.charged_at or date.today()
        ratio = Decimal(str(self._config.dispute.recoverable_ratio))
        return FineRecord(
            raw_txn_id=raw_txn_id, category=category, reason_raw=row.reason_raw,
            amount=row.penalty, charged_at=charged,
            dispute_deadline=self._deadline(charged),
            recoverable_est=(row.penalty * ratio).quantize(Decimal("0.01")),
            status="disputable",
        )

    def classify_seller(self, seller_id: uuid.UUID) -> ClassifyResult:
        seen = fines_new = skipped_no_key = skipped_not_fine = 0
        by_category: dict[str, int] = {}
        for raw in self._repo.get_unclassified_raw(seller_id):
            seen += 1
            row = normalize_row(raw.raw_json)
            if row is None:
                skipped_no_key += 1
                continue
            if not self._is_fine(row):
                skipped_not_fine += 1
                continue
            fine = self._build_fine(raw.id, row)
            if self._repo.upsert_fine(seller_id, fine):
                fines_new += 1
                by_category[fine.category] = by_category.get(fine.category, 0) + 1
        logger.info(
            "Классификация seller=%s: видно=%d новых штрафов=%d по категориям=%s",
            seller_id, fines_new, fines_new, by_category,
        )
        return ClassifyResult(
            seller_id=str(seller_id), seen=seen, fines_new=fines_new,
            by_category=by_category, skipped_no_key=skipped_no_key,
            skipped_not_fine=skipped_not_fine,
        )

    def classify_all(self) -> list[ClassifyResult]:
        return [self.classify_seller(s.id) for s in self._repo.get_active_sellers()]
