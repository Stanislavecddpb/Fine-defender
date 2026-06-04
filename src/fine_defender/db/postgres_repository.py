"""Боевая реализация Repository поверх PostgreSQL (SQLAlchemy 2.0).

Идемпотентность сырого слоя — INSERT ... ON CONFLICT DO NOTHING по
(seller_id, wb_txn_key); штрафов — по UNIQUE(raw_txn_id). Повторная выгрузка/
классификация не плодят дубли (раздел 9 ТЗ).

ВНИМАНИЕ: эта реализация написана под боевой Postgres и НЕ проверена запуском в
текущей среде (БД не поднята). Проверяется при первом боевом подключении; вся
логика MVP протестирована на InMemoryRepository.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..repository import (
    DisputeDraft,
    DisputeEvent,
    Fine,
    FineRecord,
    RawTxn,
    RawTxnRecord,
    RunHandle,
    Seller as SellerDTO,
    SummaryView,
)
from .models import (
    DisputeDraft as DisputeDraftRow,
    DisputeEvent as DisputeEventRow,
    Fine as FineRow,
    IngestionRun,
    RawTransaction,
    Seller,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PostgresRepository:
    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(database_url, pool_pre_ping=True, future=True)
        self._Session: sessionmaker[Session] = sessionmaker(self._engine, expire_on_commit=False)

    # --- здоровье ---
    def ping(self) -> None:
        with self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    # --- селлеры / кабинет ---
    @staticmethod
    def _to_seller(r: Seller) -> SellerDTO:
        return SellerDTO(
            id=r.id, name=r.name, wb_token_enc=bytes(r.wb_token_enc or b""),
            token_scopes=list(r.token_scopes or []),
            email=r.email, password_hash=r.password_hash,
        )

    def get_active_sellers(self) -> list[SellerDTO]:
        with self._Session() as s:
            rows = s.scalars(select(Seller).where(Seller.is_active.is_(True))).all()
            return [self._to_seller(r) for r in rows]

    def get_seller(self, seller_id: uuid.UUID) -> SellerDTO | None:
        with self._Session() as s:
            r = s.get(Seller, seller_id)
            return self._to_seller(r) if r else None

    def get_seller_by_email(self, email: str) -> SellerDTO | None:
        with self._Session() as s:
            r = s.scalar(select(Seller).where(Seller.email == email.strip().lower()))
            return self._to_seller(r) if r else None

    def create_seller(
        self, *, name: str | None, email: str, password_hash: str,
        wb_token_enc: bytes, token_scopes: list[str],
    ) -> SellerDTO:
        with self._Session() as s:
            obj = Seller(
                id=uuid.uuid4(), name=name, email=email.strip().lower(),
                password_hash=password_hash, wb_token_enc=wb_token_enc,
                token_scopes=token_scopes, created_at=_utcnow(), is_active=True,
            )
            s.add(obj)
            try:
                s.commit()
            except IntegrityError as exc:
                s.rollback()
                raise ValueError("email уже зарегистрирован") from exc
            return self._to_seller(obj)

    def update_token(
        self, seller_id: uuid.UUID, *, wb_token_enc: bytes, token_scopes: list[str]
    ) -> None:
        with self._Session() as s:
            obj = s.get(Seller, seller_id)
            if obj is not None:
                obj.wb_token_enc = wb_token_enc
                obj.token_scopes = token_scopes
                s.commit()

    # --- выгрузка (M1) ---
    def start_run(self, seller_id: uuid.UUID, started_at: datetime) -> RunHandle:
        with self._Session() as s:
            run = IngestionRun(seller_id=seller_id, started_at=started_at, status="running")
            s.add(run)
            s.commit()
            return RunHandle(id=run.id, seller_id=seller_id)

    def finish_run(
        self, run: RunHandle, *, status: str, rows_ingested: int,
        finished_at: datetime, error_detail: str | None = None,
    ) -> None:
        with self._Session() as s:
            obj = s.get(IngestionRun, run.id)
            if obj is None:
                return
            obj.status = status
            obj.rows_ingested = rows_ingested
            obj.finished_at = finished_at
            obj.error_detail = error_detail
            s.commit()

    def upsert_raw_transactions(
        self, seller_id: uuid.UUID, records: list[RawTxnRecord]
    ) -> int:
        if not records:
            return 0
        payload = [
            {"seller_id": seller_id, "wb_txn_key": rec.wb_txn_key,
             "raw_json": rec.raw_json, "ingested_at": _utcnow()}
            for rec in records
        ]
        stmt = (
            pg_insert(RawTransaction).values(payload)
            .on_conflict_do_nothing(index_elements=["seller_id", "wb_txn_key"])
            .returning(RawTransaction.id)
        )
        with self._Session() as s:
            inserted = s.execute(stmt).fetchall()
            s.commit()
            return len(inserted)

    # --- классификатор (M2) ---
    def get_unclassified_raw(self, seller_id: uuid.UUID) -> list[RawTxn]:
        with self._Session() as s:
            stmt = (
                select(RawTransaction)
                .outerjoin(FineRow, FineRow.raw_txn_id == RawTransaction.id)
                .where(RawTransaction.seller_id == seller_id, FineRow.id.is_(None))
            )
            rows = s.scalars(stmt).all()
            return [
                RawTxn(id=r.id, seller_id=r.seller_id, wb_txn_key=r.wb_txn_key, raw_json=r.raw_json)
                for r in rows
            ]

    def upsert_fine(self, seller_id: uuid.UUID, fine: FineRecord) -> bool:
        now = _utcnow()
        stmt = (
            pg_insert(FineRow)
            .values(
                seller_id=seller_id, raw_txn_id=fine.raw_txn_id, category=fine.category,
                reason_raw=fine.reason_raw, amount=fine.amount, charged_at=fine.charged_at,
                dispute_deadline=fine.dispute_deadline, recoverable_est=fine.recoverable_est,
                status=fine.status, discrepancy_flag=fine.discrepancy_flag,
                created_at=now, updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["raw_txn_id"])
            .returning(FineRow.id)
        )
        with self._Session() as s:
            res = s.execute(stmt).fetchone()
            s.commit()
            return res is not None

    def expire_overdue(self, today: date) -> int:
        with self._Session() as s:
            ids = s.scalars(
                select(FineRow.id).where(
                    FineRow.status.in_(("disputable", "drafting")),
                    FineRow.dispute_deadline.is_not(None),
                    FineRow.dispute_deadline < today,
                )
            ).all()
        for fid in ids:
            self.add_dispute_event(
                fid, status="expired", note="Автостатус: дедлайн оспаривания пропущен"
            )
        return len(ids)

    # --- дашборд (M3) ---
    @staticmethod
    def _to_fine(r: FineRow) -> Fine:
        return Fine(
            id=r.id, seller_id=r.seller_id, raw_txn_id=r.raw_txn_id, category=r.category,
            reason_raw=r.reason_raw or "", amount=r.amount, charged_at=r.charged_at,
            dispute_deadline=r.dispute_deadline, recoverable_est=r.recoverable_est,
            status=r.status, discrepancy_flag=r.discrepancy_flag,
            created_at=r.created_at, updated_at=r.updated_at,
        )

    def list_fines(
        self, seller_id: uuid.UUID, *, status: str | None = None, category: str | None = None
    ) -> list[Fine]:
        with self._Session() as s:
            stmt = select(FineRow).where(FineRow.seller_id == seller_id)
            if status:
                stmt = stmt.where(FineRow.status == status)
            if category:
                stmt = stmt.where(FineRow.category == category)
            stmt = stmt.order_by(FineRow.dispute_deadline.asc().nulls_last())
            return [self._to_fine(r) for r in s.scalars(stmt).all()]

    def get_fine(self, fine_id: uuid.UUID) -> Fine | None:
        with self._Session() as s:
            r = s.get(FineRow, fine_id)
            return self._to_fine(r) if r else None

    def get_raw_txn(self, raw_txn_id: int) -> RawTxn | None:
        with self._Session() as s:
            r = s.get(RawTransaction, raw_txn_id)
            return RawTxn(id=r.id, seller_id=r.seller_id, wb_txn_key=r.wb_txn_key,
                         raw_json=r.raw_json) if r else None

    def summary(self, seller_id: uuid.UUID) -> SummaryView:
        active = ("disputable", "drafting", "submitted")
        with self._Session() as s:
            recoverable = s.scalar(
                select(func.coalesce(func.sum(FineRow.recoverable_est), 0))
                .where(FineRow.seller_id == seller_id, FineRow.status.in_(active))
            ) or Decimal("0")
            recovered = s.scalar(
                select(func.coalesce(func.sum(DisputeEventRow.recovered_amount), 0))
                .join(FineRow, FineRow.id == DisputeEventRow.fine_id)
                .where(FineRow.seller_id == seller_id, DisputeEventRow.status == "won")
            ) or Decimal("0")
            fines_total = s.scalar(
                select(func.count()).select_from(FineRow).where(FineRow.seller_id == seller_id)
            ) or 0
            disputable_total = s.scalar(
                select(func.count()).select_from(FineRow)
                .where(FineRow.seller_id == seller_id, FineRow.status == "disputable")
            ) or 0
        return SummaryView(
            recoverable_total=Decimal(recoverable), recovered_total=Decimal(recovered),
            fines_total=int(fines_total), disputable_total=int(disputable_total),
        )

    def add_dispute_event(
        self, fine_id: uuid.UUID, *, status: str,
        recovered_amount: Decimal | None = None, note: str | None = None,
    ) -> DisputeEvent:
        now = _utcnow()
        with self._Session() as s:
            ev = DisputeEventRow(
                fine_id=fine_id, status=status, recovered_amount=recovered_amount,
                note=note, created_at=now,
            )
            s.add(ev)
            fine = s.get(FineRow, fine_id)
            if fine is not None:
                fine.status = status
                fine.updated_at = now
            s.commit()
            return DisputeEvent(id=ev.id, fine_id=fine_id, status=status,
                                recovered_amount=recovered_amount, note=note, created_at=now)

    def list_dispute_events(self, fine_id: uuid.UUID) -> list[DisputeEvent]:
        with self._Session() as s:
            rows = s.scalars(
                select(DisputeEventRow).where(DisputeEventRow.fine_id == fine_id)
                .order_by(DisputeEventRow.created_at.asc())
            ).all()
            return [
                DisputeEvent(id=r.id, fine_id=r.fine_id, status=r.status,
                             recovered_amount=r.recovered_amount, note=r.note,
                             created_at=r.created_at)
                for r in rows
            ]

    # --- генератор претензии (M4) ---
    def save_dispute_draft(
        self, fine_id: uuid.UUID, body_md: str, evidence_checklist: list[str]
    ) -> DisputeDraft:
        now = _utcnow()
        with self._Session() as s:
            draft = DisputeDraftRow(
                fine_id=fine_id, body_md=body_md, evidence_checklist=evidence_checklist,
                generated_at=now,
            )
            s.add(draft)
            s.commit()
            return DisputeDraft(id=draft.id, fine_id=fine_id, body_md=body_md,
                                evidence_checklist=evidence_checklist, generated_at=now)

    def get_dispute_draft(self, fine_id: uuid.UUID) -> DisputeDraft | None:
        with self._Session() as s:
            r = s.scalar(
                select(DisputeDraftRow).where(DisputeDraftRow.fine_id == fine_id)
                .order_by(DisputeDraftRow.generated_at.desc()).limit(1)
            )
            return DisputeDraft(id=r.id, fine_id=r.fine_id, body_md=r.body_md,
                                evidence_checklist=list(r.evidence_checklist),
                                generated_at=r.generated_at) if r else None
