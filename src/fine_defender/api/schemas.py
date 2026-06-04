"""Pydantic-схемы ответов дашборда."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class SellerOut(BaseModel):
    id: uuid.UUID
    name: str | None


class FineOut(BaseModel):
    id: uuid.UUID
    category: str
    reason_raw: str
    amount: Decimal
    charged_at: date
    dispute_deadline: date | None
    recoverable_est: Decimal | None
    status: str
    discrepancy_flag: bool
    days_to_deadline: int | None
    deadline_soon: bool


class DisputeEventOut(BaseModel):
    status: str
    recovered_amount: Decimal | None
    note: str | None
    created_at: datetime


class DisputeDraftOut(BaseModel):
    body_md: str
    evidence_checklist: list[str]
    generated_at: datetime


class FineCardOut(BaseModel):
    fine: FineOut
    raw_json: dict
    draft: DisputeDraftOut | None
    events: list[DisputeEventOut]


class SummaryOut(BaseModel):
    recoverable_total: Decimal
    recovered_total: Decimal
    fines_total: int
    disputable_total: int


class EventIn(BaseModel):
    status: str
    recovered_amount: Decimal | None = None
    note: str | None = None
