"""Интеграционный тест на реальном Postgres (PostgresRepository).

Opt-in: запускается только если задан PG_TEST_DSN (SQLAlchemy-DSN до тестовой БД).
Без него тест пропускается, поэтому обычный `pytest` (юнит-тесты) не требует БД.

Изоляция по тенанту: тест создаёт собственного селлера и удаляет все его данные
в teardown — не зависит от демо-данных и не мусорит в БД.

Запуск (БД из docker-compose):
  PG_TEST_DSN=postgresql+psycopg://fine_defender:<pw>@localhost:5432/fine_defender pytest tests/test_pg_integration.py
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

DSN = os.environ.get("PG_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="PG_TEST_DSN не задан — пропуск интеграции с Postgres")

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "weekly_report_demo.json"


class _NoopCipher:
    def decrypt(self, blob: bytes) -> str:
        return "mock-token"


def _cleanup(engine, seller_id: uuid.UUID) -> None:
    with engine.begin() as c:
        sid = str(seller_id)
        c.execute(text("DELETE FROM dispute_events WHERE fine_id IN (SELECT id FROM fines WHERE seller_id=:s)"), {"s": sid})
        c.execute(text("DELETE FROM dispute_drafts WHERE fine_id IN (SELECT id FROM fines WHERE seller_id=:s)"), {"s": sid})
        c.execute(text("DELETE FROM fines WHERE seller_id=:s"), {"s": sid})
        c.execute(text("DELETE FROM raw_transactions WHERE seller_id=:s"), {"s": sid})
        c.execute(text("DELETE FROM ingestion_runs WHERE seller_id=:s"), {"s": sid})
        c.execute(text("DELETE FROM sellers WHERE id=:s"), {"s": sid})


@pytest.fixture
def pg():
    from fine_defender.db.models import Seller
    from fine_defender.db.postgres_repository import PostgresRepository

    engine = create_engine(DSN, future=True)
    seller_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(Seller(
            id=seller_id, name="PYTEST", wb_token_enc=b"", token_scopes=["read"],
            created_at=datetime.now(timezone.utc), is_active=True,
        ))
        s.commit()
    repo = PostgresRepository(DSN)
    try:
        yield repo, seller_id
    finally:
        _cleanup(engine, seller_id)
        engine.dispose()


def _rows() -> list[dict]:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def test_full_pipeline_on_postgres(pg):
    from fine_defender.classifier import Classifier
    from fine_defender.config import get_app_config
    from fine_defender.dispute.generator import DisputeGenerator
    from fine_defender.ingestion.worker import IngestionWorker
    from fine_defender.repository import Seller as SellerDTO
    from fine_defender.wb.client import MockReportClient

    repo, seller_id = pg
    cfg = get_app_config()
    seller = SellerDTO(id=seller_id, name="PYTEST", wb_token_enc=b"", token_scopes=["read"])

    # M1: выгрузка в реальную БД
    res = IngestionWorker(repo, MockReportClient(_rows()), config=cfg, cipher=_NoopCipher()).run_for_seller(seller)
    assert res.rows_new == 11
    assert res.rows_skipped_no_key == 1

    # идемпотентность сырого слоя
    res2 = IngestionWorker(repo, MockReportClient(_rows()), config=cfg, cipher=_NoopCipher()).run_for_seller(seller)
    assert res2.rows_new == 0

    # M2: классификация
    clf = Classifier(repo, config=cfg).classify_seller(seller_id)
    assert clf.fines_new == 8
    assert clf.by_category == {"oversize_logistics": 5, "other": 3}
    # идемпотентность классификации
    assert Classifier(repo, config=cfg).classify_seller(seller_id).fines_new == 0

    # дашборд из БД
    oversize = repo.list_fines(seller_id, category="oversize_logistics")
    assert len(oversize) == 5
    summary = repo.summary(seller_id)
    assert summary.recoverable_total == Decimal("6540.50")
    assert summary.disputable_total == 5

    # M4: черновик + действие оператора (запись в БД)
    fine = oversize[0]
    draft = DisputeGenerator(repo, config=cfg).generate_for_fine(fine.id, "PYTEST")
    assert len(draft.evidence_checklist) == 5
    assert repo.get_dispute_draft(fine.id) is not None
    assert repo.get_fine(fine.id).status == "drafting"

    repo.add_dispute_event(fine.id, status="won", recovered_amount=Decimal("780.00"), note="ok")
    assert repo.summary(seller_id).recovered_total == Decimal("780.00")
    assert repo.get_fine(fine.id).status == "won"


def test_expire_overdue_on_postgres(pg):
    """Авто-просрочка на реальном Postgres (относительные даты — устойчиво во времени)."""
    from datetime import timedelta

    from fine_defender.repository import FineRecord, RawTxnRecord

    repo, seller_id = pg
    today = date.today()

    repo.upsert_raw_transactions(seller_id, [
        RawTxnRecord(wb_txn_key="exp-past", raw_json={}),
        RawTxnRecord(wb_txn_key="exp-future", raw_json={}),
    ])
    raws = {r.wb_txn_key: r.id for r in repo.get_unclassified_raw(seller_id)}

    repo.upsert_fine(seller_id, FineRecord(
        raw_txn_id=raws["exp-past"], category="oversize_logistics", reason_raw="r",
        amount=Decimal("500"), charged_at=today - timedelta(days=40),
        dispute_deadline=today - timedelta(days=1), recoverable_est=Decimal("500"),
        status="disputable",
    ))
    repo.upsert_fine(seller_id, FineRecord(
        raw_txn_id=raws["exp-future"], category="oversize_logistics", reason_raw="r",
        amount=Decimal("700"), charged_at=today - timedelta(days=5),
        dispute_deadline=today + timedelta(days=10), recoverable_est=Decimal("700"),
        status="disputable",
    ))

    assert repo.expire_overdue(today) == 1
    statuses = {f.amount: f.status for f in repo.list_fines(seller_id)}
    assert statuses[Decimal("500.00")] == "expired"
    assert statuses[Decimal("700.00")] == "disputable"
    # повторно — идемпотентно
    assert repo.expire_overdue(today) == 0
