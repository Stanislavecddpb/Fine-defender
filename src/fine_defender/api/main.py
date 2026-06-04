"""FastAPI-приложение (раздел 8 ТЗ): минимальный read-only дашборд + действия
оператора (смена статуса, ввод recovered_amount) и генерация черновика претензии.

Без публичной регистрации — селлеры заводятся вручную (scripts/seed_seller.py).
Бэкенд (memory/postgres) выбирается в bootstrap по APP_BACKEND.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from .. import __version__
from ..config import get_app_config
from ..dispute.generator import DisputeGenerator, DraftError
from ..logging_utils import configure_logging
from ..repository import Fine, Repository
from .bootstrap import build_repository
from .schemas import (
    DisputeDraftOut,
    DisputeEventOut,
    EventIn,
    FineCardOut,
    FineOut,
    SellerOut,
    SummaryOut,
)

configure_logging()

app = FastAPI(title="Fine Defender", version=__version__)
_repo: Repository = build_repository()


def get_repo() -> Repository:
    return _repo


def _to_fine_out(fine: Fine) -> FineOut:
    warn_days = get_app_config().dispute.deadline_warning_days
    days_left: int | None = None
    soon = False
    if fine.dispute_deadline is not None:
        days_left = (fine.dispute_deadline - date.today()).days
        soon = days_left <= warn_days
    return FineOut(
        id=fine.id, category=fine.category, reason_raw=fine.reason_raw,
        amount=fine.amount, charged_at=fine.charged_at,
        dispute_deadline=fine.dispute_deadline, recoverable_est=fine.recoverable_est,
        status=fine.status, discrepancy_flag=fine.discrepancy_flag,
        days_to_deadline=days_left, deadline_soon=soon,
    )


logger = logging.getLogger(__name__)

_DASHBOARD = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    """Визуальный дашборд (читает /api/* того же origin)."""
    return FileResponse(_DASHBOARD)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: процесс жив. Не трогает БД."""
    return {"status": "ok", "version": __version__}


@app.get("/health/ready")
def ready(repo: Repository = Depends(get_repo)) -> JSONResponse:
    """Readiness: проверяет доступность хранилища. 503, если БД недоступна."""
    try:
        repo.ping()
    except Exception as exc:  # noqa: BLE001 — наружу деталь не отдаём
        logger.warning("Readiness probe failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(status_code=200, content={"status": "ready", "version": __version__})


@app.get("/api/sellers", response_model=list[SellerOut])
def list_sellers(repo: Repository = Depends(get_repo)) -> list[SellerOut]:
    return [SellerOut(id=s.id, name=s.name) for s in repo.get_active_sellers()]


@app.get("/api/fines", response_model=list[FineOut])
def list_fines(
    seller_id: uuid.UUID = Query(...),
    status: str | None = Query(None),
    category: str | None = Query(None),
    repo: Repository = Depends(get_repo),
) -> list[FineOut]:
    fines = repo.list_fines(seller_id, status=status, category=category)
    return [_to_fine_out(f) for f in fines]


@app.get("/api/summary", response_model=SummaryOut)
def summary(
    seller_id: uuid.UUID = Query(...), repo: Repository = Depends(get_repo)
) -> SummaryOut:
    s = repo.summary(seller_id)
    return SummaryOut(
        recoverable_total=s.recoverable_total, recovered_total=s.recovered_total,
        fines_total=s.fines_total, disputable_total=s.disputable_total,
    )


@app.get("/api/fines/{fine_id}", response_model=FineCardOut)
def fine_card(fine_id: uuid.UUID, repo: Repository = Depends(get_repo)) -> FineCardOut:
    fine = repo.get_fine(fine_id)
    if fine is None:
        raise HTTPException(status_code=404, detail="Штраф не найден")
    raw = repo.get_raw_txn(fine.raw_txn_id)
    draft = repo.get_dispute_draft(fine_id)
    events = repo.list_dispute_events(fine_id)
    return FineCardOut(
        fine=_to_fine_out(fine),
        raw_json=(raw.raw_json if raw else {}),
        draft=(
            DisputeDraftOut(
                body_md=draft.body_md, evidence_checklist=draft.evidence_checklist,
                generated_at=draft.generated_at,
            ) if draft else None
        ),
        events=[
            DisputeEventOut(
                status=e.status, recovered_amount=e.recovered_amount,
                note=e.note, created_at=e.created_at,
            ) for e in events
        ],
    )


@app.post("/api/fines/{fine_id}/events", response_model=DisputeEventOut)
def add_event(
    fine_id: uuid.UUID, payload: EventIn, repo: Repository = Depends(get_repo)
) -> DisputeEventOut:
    """Действие оператора: сменить статус, внести recovered_amount, добавить заметку."""
    if repo.get_fine(fine_id) is None:
        raise HTTPException(status_code=404, detail="Штраф не найден")
    e = repo.add_dispute_event(
        fine_id, status=payload.status,
        recovered_amount=payload.recovered_amount, note=payload.note,
    )
    return DisputeEventOut(
        status=e.status, recovered_amount=e.recovered_amount,
        note=e.note, created_at=e.created_at,
    )


@app.post("/api/fines/{fine_id}/draft", response_model=DisputeDraftOut)
def generate_draft(
    fine_id: uuid.UUID, repo: Repository = Depends(get_repo)
) -> DisputeDraftOut:
    """Сгенерировать черновик претензии (M4) и привязать к карточке штрафа."""
    fine = repo.get_fine(fine_id)
    if fine is None:
        raise HTTPException(status_code=404, detail="Штраф не найден")
    seller = next(
        (s for s in repo.get_active_sellers() if s.id == fine.seller_id), None
    )
    try:
        draft = DisputeGenerator(repo).generate_for_fine(
            fine_id, seller.name if seller else None
        )
    except DraftError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return DisputeDraftOut(
        body_md=draft.body_md, evidence_checklist=draft.evidence_checklist,
        generated_at=draft.generated_at,
    )
