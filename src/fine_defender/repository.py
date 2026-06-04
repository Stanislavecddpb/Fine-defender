"""Слой хранения: протокол + in-memory реализация для моков/тестов.

Сервисы (выгрузка, классификатор, генератор, дашборд) зависят от Repository
(абстракция), а не от Postgres напрямую. Это позволяет гонять весь пайплайн на
моках без поднятой БД и тестировать логику в памяти. Боевая реализация —
db/postgres_repository.py.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Protocol


@dataclass
class Seller:
    id: uuid.UUID
    name: str | None
    wb_token_enc: bytes
    token_scopes: list[str]
    email: str | None = None
    password_hash: str | None = None

    @property
    def has_token(self) -> bool:
        return bool(self.wb_token_enc)


@dataclass
class RawTxnRecord:
    """Запись для сырого слоя (append-only)."""

    wb_txn_key: str
    raw_json: dict[str, Any]
    report_period: tuple[date, date] | None = None


@dataclass
class RawTxn:
    """Сохранённая сырая транзакция (с присвоенным id)."""

    id: int
    seller_id: uuid.UUID
    wb_txn_key: str
    raw_json: dict[str, Any]


@dataclass
class FineRecord:
    """Нормализованный штраф для записи (выход классификатора)."""

    raw_txn_id: int
    category: str
    reason_raw: str
    amount: Decimal
    charged_at: date
    dispute_deadline: date | None
    recoverable_est: Decimal | None
    status: str
    discrepancy_flag: bool = False


@dataclass
class Fine:
    """Штраф для чтения (дашборд)."""

    id: uuid.UUID
    seller_id: uuid.UUID
    raw_txn_id: int
    category: str
    reason_raw: str
    amount: Decimal
    charged_at: date
    dispute_deadline: date | None
    recoverable_est: Decimal | None
    status: str
    discrepancy_flag: bool
    created_at: datetime
    updated_at: datetime


@dataclass
class DisputeEvent:
    id: uuid.UUID
    fine_id: uuid.UUID
    status: str
    recovered_amount: Decimal | None
    note: str | None
    created_at: datetime


@dataclass
class DisputeDraft:
    id: uuid.UUID
    fine_id: uuid.UUID
    body_md: str
    evidence_checklist: list[str]
    generated_at: datetime


@dataclass
class RunHandle:
    id: uuid.UUID
    seller_id: uuid.UUID


@dataclass
class SummaryView:
    recoverable_total: Decimal
    recovered_total: Decimal
    fines_total: int
    disputable_total: int


class Repository(Protocol):
    # --- здоровье ---
    def ping(self) -> None:
        """Проверка доступности хранилища (для readiness-проб). Бросает при сбое."""
        ...

    # --- селлеры / кабинет ---
    def get_active_sellers(self) -> list[Seller]: ...
    def get_seller(self, seller_id: uuid.UUID) -> Seller | None: ...
    def get_seller_by_email(self, email: str) -> Seller | None: ...
    def create_seller(
        self, *, name: str | None, email: str, password_hash: str,
        wb_token_enc: bytes, token_scopes: list[str],
    ) -> Seller: ...
    def update_token(
        self, seller_id: uuid.UUID, *, wb_token_enc: bytes, token_scopes: list[str]
    ) -> None: ...

    # --- выгрузка (M1) ---
    def start_run(self, seller_id: uuid.UUID, started_at: datetime) -> RunHandle: ...
    def finish_run(
        self, run: RunHandle, *, status: str, rows_ingested: int,
        finished_at: datetime, error_detail: str | None = None,
    ) -> None: ...
    def upsert_raw_transactions(
        self, seller_id: uuid.UUID, records: list[RawTxnRecord]
    ) -> int: ...

    # --- классификатор (M2) ---
    def get_unclassified_raw(self, seller_id: uuid.UUID) -> list[RawTxn]: ...
    def upsert_fine(self, seller_id: uuid.UUID, fine: FineRecord) -> bool: ...
    def expire_overdue(self, today: date) -> int:
        """Перевести в 'expired' оспоримые штрафы с пропущенным дедлайном. Возвращает число."""
        ...

    # --- дашборд (M3) ---
    def list_fines(
        self, seller_id: uuid.UUID, *, status: str | None = None,
        category: str | None = None,
    ) -> list[Fine]: ...
    def get_fine(self, fine_id: uuid.UUID) -> Fine | None: ...
    def get_raw_txn(self, raw_txn_id: int) -> RawTxn | None: ...
    def summary(self, seller_id: uuid.UUID) -> SummaryView: ...
    def add_dispute_event(
        self, fine_id: uuid.UUID, *, status: str,
        recovered_amount: Decimal | None = None, note: str | None = None,
    ) -> DisputeEvent: ...
    def list_dispute_events(self, fine_id: uuid.UUID) -> list[DisputeEvent]: ...

    # --- генератор претензии (M4) ---
    def save_dispute_draft(
        self, fine_id: uuid.UUID, body_md: str, evidence_checklist: list[str]
    ) -> DisputeDraft: ...
    def get_dispute_draft(self, fine_id: uuid.UUID) -> DisputeDraft | None: ...


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- In-memory реализация (моки/тесты/локальный демо-сервер) ---


class InMemoryRepository:
    def __init__(self, sellers: list[Seller] | None = None) -> None:
        self.sellers: list[Seller] = sellers or []
        self.raw: list[RawTxn] = []
        self._raw_keys: set[tuple[uuid.UUID, str]] = set()
        self._raw_seq = 0
        self.runs: list[dict[str, Any]] = []
        self.fines: dict[uuid.UUID, Fine] = {}
        self._fine_by_raw: dict[int, uuid.UUID] = {}
        self.events: list[DisputeEvent] = []
        self.drafts: dict[uuid.UUID, DisputeDraft] = {}

    # здоровье
    def ping(self) -> None:
        return None

    # селлеры / кабинет
    def get_active_sellers(self) -> list[Seller]:
        return list(self.sellers)

    def get_seller(self, seller_id: uuid.UUID) -> Seller | None:
        return next((s for s in self.sellers if s.id == seller_id), None)

    def get_seller_by_email(self, email: str) -> Seller | None:
        e = email.strip().lower()
        return next((s for s in self.sellers if (s.email or "").lower() == e), None)

    def create_seller(
        self, *, name: str | None, email: str, password_hash: str,
        wb_token_enc: bytes, token_scopes: list[str],
    ) -> Seller:
        if self.get_seller_by_email(email) is not None:
            raise ValueError("email уже зарегистрирован")
        seller = Seller(
            id=uuid.uuid4(), name=name, wb_token_enc=wb_token_enc,
            token_scopes=token_scopes, email=email.strip().lower(),
            password_hash=password_hash,
        )
        self.sellers.append(seller)
        return seller

    def update_token(
        self, seller_id: uuid.UUID, *, wb_token_enc: bytes, token_scopes: list[str]
    ) -> None:
        s = self.get_seller(seller_id)
        if s is not None:
            s.wb_token_enc = wb_token_enc
            s.token_scopes = token_scopes

    # выгрузка
    def start_run(self, seller_id: uuid.UUID, started_at: datetime) -> RunHandle:
        handle = RunHandle(id=uuid.uuid4(), seller_id=seller_id)
        self.runs.append({
            "id": handle.id, "seller_id": seller_id, "started_at": started_at,
            "status": "running", "rows_ingested": None, "finished_at": None,
            "error_detail": None,
        })
        return handle

    def finish_run(
        self, run: RunHandle, *, status: str, rows_ingested: int,
        finished_at: datetime, error_detail: str | None = None,
    ) -> None:
        for r in self.runs:
            if r["id"] == run.id:
                r.update(status=status, rows_ingested=rows_ingested,
                         finished_at=finished_at, error_detail=error_detail)
                return

    def upsert_raw_transactions(
        self, seller_id: uuid.UUID, records: list[RawTxnRecord]
    ) -> int:
        new_count = 0
        for rec in records:
            key = (seller_id, rec.wb_txn_key)
            if key in self._raw_keys:  # append-only: дубли не пишем
                continue
            self._raw_keys.add(key)
            self._raw_seq += 1
            self.raw.append(RawTxn(
                id=self._raw_seq, seller_id=seller_id,
                wb_txn_key=rec.wb_txn_key, raw_json=rec.raw_json,
            ))
            new_count += 1
        return new_count

    # классификатор
    def get_unclassified_raw(self, seller_id: uuid.UUID) -> list[RawTxn]:
        return [
            r for r in self.raw
            if r.seller_id == seller_id and r.id not in self._fine_by_raw
        ]

    # авто-просрочка: оспоримые штрафы с пропущенным дедлайном → 'expired'
    _EXPIRABLE = ("disputable", "drafting")

    def expire_overdue(self, today: date) -> int:
        overdue = [
            f.id for f in self.fines.values()
            if f.status in self._EXPIRABLE
            and f.dispute_deadline is not None
            and f.dispute_deadline < today
        ]
        for fid in overdue:
            self.add_dispute_event(
                fid, status="expired", note="Автостатус: дедлайн оспаривания пропущен"
            )
        return len(overdue)

    def upsert_fine(self, seller_id: uuid.UUID, fine: FineRecord) -> bool:
        if fine.raw_txn_id in self._fine_by_raw:  # идемпотентность по raw_txn_id
            return False
        now = utcnow()
        obj = Fine(
            id=uuid.uuid4(), seller_id=seller_id, raw_txn_id=fine.raw_txn_id,
            category=fine.category, reason_raw=fine.reason_raw, amount=fine.amount,
            charged_at=fine.charged_at, dispute_deadline=fine.dispute_deadline,
            recoverable_est=fine.recoverable_est, status=fine.status,
            discrepancy_flag=fine.discrepancy_flag, created_at=now, updated_at=now,
        )
        self.fines[obj.id] = obj
        self._fine_by_raw[fine.raw_txn_id] = obj.id
        return True

    # дашборд
    def list_fines(
        self, seller_id: uuid.UUID, *, status: str | None = None,
        category: str | None = None,
    ) -> list[Fine]:
        items = [f for f in self.fines.values() if f.seller_id == seller_id]
        if status:
            items = [f for f in items if f.status == status]
        if category:
            items = [f for f in items if f.category == category]
        # ближайший дедлайн — выше
        return sorted(items, key=lambda f: (f.dispute_deadline or date.max))

    def get_fine(self, fine_id: uuid.UUID) -> Fine | None:
        return self.fines.get(fine_id)

    def get_raw_txn(self, raw_txn_id: int) -> RawTxn | None:
        return next((r for r in self.raw if r.id == raw_txn_id), None)

    def summary(self, seller_id: uuid.UUID) -> SummaryView:
        fines = [f for f in self.fines.values() if f.seller_id == seller_id]
        active_statuses = {"disputable", "drafting", "submitted"}
        recoverable = sum(
            (f.recoverable_est or Decimal("0"))
            for f in fines if f.status in active_statuses
        )
        # «Отбито» — один раз на штраф: берём сумму из ПОСЛЕДНЕГО события won
        # каждого выигранного штрафа (повторные клики не накапливают сумму).
        recovered = Decimal("0")
        for f in fines:
            if f.status != "won":
                continue
            amt = next(
                (e.recovered_amount for e in reversed(self.events)
                 if e.fine_id == f.id and e.status == "won" and e.recovered_amount is not None),
                None,
            )
            recovered += amt or Decimal("0")
        return SummaryView(
            recoverable_total=Decimal(recoverable),
            recovered_total=Decimal(recovered),
            fines_total=len(fines),
            disputable_total=sum(1 for f in fines if f.status == "disputable"),
        )

    def add_dispute_event(
        self, fine_id: uuid.UUID, *, status: str,
        recovered_amount: Decimal | None = None, note: str | None = None,
    ) -> DisputeEvent:
        # Дедуп: повторный клик с тем же статусом и суммой не плодит события.
        last = next((e for e in reversed(self.events) if e.fine_id == fine_id), None)
        if last is not None and last.status == status and last.recovered_amount == recovered_amount:
            return last
        event = DisputeEvent(
            id=uuid.uuid4(), fine_id=fine_id, status=status,
            recovered_amount=recovered_amount, note=note, created_at=utcnow(),
        )
        self.events.append(event)
        fine = self.fines.get(fine_id)
        if fine is not None:  # ручная смена статуса оператором двигает и сам штраф
            fine.status = status
            fine.updated_at = utcnow()
        return event

    def list_dispute_events(self, fine_id: uuid.UUID) -> list[DisputeEvent]:
        return [e for e in self.events if e.fine_id == fine_id]

    # генератор претензии
    def save_dispute_draft(
        self, fine_id: uuid.UUID, body_md: str, evidence_checklist: list[str]
    ) -> DisputeDraft:
        draft = DisputeDraft(
            id=uuid.uuid4(), fine_id=fine_id, body_md=body_md,
            evidence_checklist=evidence_checklist, generated_at=utcnow(),
        )
        self.drafts[fine_id] = draft
        return draft

    def get_dispute_draft(self, fine_id: uuid.UUID) -> DisputeDraft | None:
        return self.drafts.get(fine_id)
