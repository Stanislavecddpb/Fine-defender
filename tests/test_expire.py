"""Тесты авто-перевода просроченных штрафов в 'expired'."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from fine_defender.repository import FineRecord, InMemoryRepository, Seller


def _repo_with_raw(n: int = 5):
    seller = Seller(id=uuid.uuid4(), name="T", wb_token_enc=b"", token_scopes=["read"])
    repo = InMemoryRepository(sellers=[seller])
    from fine_defender.repository import RawTxnRecord
    repo.upsert_raw_transactions(
        seller.id, [RawTxnRecord(wb_txn_key=str(i), raw_json={"i": i}) for i in range(1, n + 1)]
    )
    return repo, seller


def _add_fine(repo, seller_id, raw_txn_id, *, deadline, status="disputable"):
    repo.upsert_fine(seller_id, FineRecord(
        raw_txn_id=raw_txn_id, category="oversize_logistics", reason_raw="r",
        amount=Decimal("100"), charged_at=date(2026, 1, 1), dispute_deadline=deadline,
        recoverable_est=Decimal("100"), status=status,
    ))
    # upsert_fine не задаёт статус из аргумента в готовый объект? задаёт — см. FineRecord.status
    return repo.list_fines(seller_id)[-1]


def test_overdue_disputable_becomes_expired():
    repo, seller = _repo_with_raw()
    today = date.today()
    _add_fine(repo, seller.id, 1, deadline=today - timedelta(days=1))   # просрочен
    _add_fine(repo, seller.id, 2, deadline=today + timedelta(days=5))   # ещё в запасе

    n = repo.expire_overdue(today)
    assert n == 1
    statuses = {f.raw_txn_id: f.status for f in repo.list_fines(seller.id)}
    assert statuses[1] == "expired"
    assert statuses[2] == "disputable"


def test_expire_logs_event():
    repo, seller = _repo_with_raw()
    f = _add_fine(repo, seller.id, 1, deadline=date.today() - timedelta(days=3))
    repo.expire_overdue(date.today())
    events = repo.list_dispute_events(f.id)
    assert any(e.status == "expired" for e in events)


def test_submitted_and_won_not_expired():
    repo, seller = _repo_with_raw()
    past = date.today() - timedelta(days=10)
    _add_fine(repo, seller.id, 1, deadline=past, status="submitted")
    _add_fine(repo, seller.id, 2, deadline=past, status="won")
    assert repo.expire_overdue(date.today()) == 0
    statuses = {f.raw_txn_id: f.status for f in repo.list_fines(seller.id)}
    assert statuses[1] == "submitted"
    assert statuses[2] == "won"


def test_expire_is_idempotent():
    repo, seller = _repo_with_raw()
    _add_fine(repo, seller.id, 1, deadline=date.today() - timedelta(days=1))
    assert repo.expire_overdue(date.today()) == 1
    assert repo.expire_overdue(date.today()) == 0  # уже expired — повторно не трогаем
