"""FastAPI-приложение: личный кабинет селлера.

Авторизация по сессии (подписанная cookie), самостоятельная регистрация, ввод
WB-токена и запуск проверки. Все данные строго изолированы по тенанту: seller_id
берётся ИЗ СЕССИИ, а не из запроса; доступ к чужому объекту → 404 (защита от IDOR).
См. docs/TZ_addendum_cabinet.md.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from .. import __version__
from ..classifier import Classifier
from ..config import get_app_config, get_settings
from ..crypto import TokenCipher
from ..dispute.generator import DisputeGenerator, DraftError
from ..ingestion.worker import IngestionWorker
from ..logging_utils import configure_logging
from ..repository import Fine, Repository, Seller
from ..security import hash_password, verify_password
from ..wb.client import build_report_client
from .bootstrap import build_repository
from .schemas import (
    DisputeDraftOut,
    DisputeEventOut,
    EventIn,
    FineCardOut,
    FineOut,
    IngestRunOut,
    LoginIn,
    MeOut,
    RegisterIn,
    SummaryOut,
    TokenIn,
)

configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title="Fine Defender", version=__version__)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_settings().session_secret,
    same_site="lax",
    https_only=False,  # за TLS-прокси в проде включить через переменную окружения
)
_repo: Repository = build_repository()

_DASHBOARD = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"


def get_repo() -> Repository:
    return _repo


def current_seller(request: Request, repo: Repository = Depends(get_repo)) -> Seller:
    """Текущий селлер из сессии. 401, если не авторизован/сессия протухла."""
    sid = request.session.get("seller_id")
    if not sid:
        raise HTTPException(status_code=401, detail="Требуется вход")
    seller = repo.get_seller(uuid.UUID(sid))
    if seller is None:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Сессия недействительна")
    return seller


class _StubCipher:
    """Заглушка дешифровки для mock-режима (токен WB не используется)."""

    def decrypt(self, blob: bytes) -> str:
        return "mock-token"


def _me(seller: Seller) -> MeOut:
    return MeOut(
        id=seller.id, name=seller.name, email=seller.email,
        has_token=seller.has_token, token_scopes=list(seller.token_scopes or []),
    )


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


def _own_fine_or_404(repo: Repository, seller: Seller, fine_id: uuid.UUID) -> Fine:
    fine = repo.get_fine(fine_id)
    # 404 (не 403) для чужого/несуществующего — не раскрываем существование объекта
    if fine is None or fine.seller_id != seller.id:
        raise HTTPException(status_code=404, detail="Штраф не найден")
    return fine


# --- статика / здоровье ---


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(_DASHBOARD)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/health/ready")
def ready(repo: Repository = Depends(get_repo)) -> JSONResponse:
    try:
        repo.ping()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Readiness probe failed: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(status_code=200, content={"status": "ready", "version": __version__})


# --- авторизация ---


def _encrypt_token(raw: str) -> bytes:
    return TokenCipher().encrypt(raw)


@app.post("/auth/register", response_model=MeOut)
def register(payload: RegisterIn, request: Request, repo: Repository = Depends(get_repo)) -> MeOut:
    email = payload.email.strip().lower()
    if "@" not in email or "." not in email:
        raise HTTPException(status_code=422, detail="Некорректный email")
    try:
        pwd_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    token_enc = b""
    scopes: list[str] = []
    if payload.wb_token:
        token_enc = _encrypt_token(payload.wb_token.strip())
        scopes = ["read"]
    try:
        seller = repo.create_seller(
            name=payload.company_name, email=email, password_hash=pwd_hash,
            wb_token_enc=token_enc, token_scopes=scopes,
        )
    except ValueError:
        raise HTTPException(status_code=409, detail="Email уже зарегистрирован") from None
    request.session["seller_id"] = str(seller.id)
    logger.info("Регистрация селлера id=%s", seller.id)
    return _me(seller)


@app.post("/auth/login", response_model=MeOut)
def login(payload: LoginIn, request: Request, repo: Repository = Depends(get_repo)) -> MeOut:
    seller = repo.get_seller_by_email(payload.email.strip().lower())
    # единый ответ при неверных кредах — не раскрываем существование email
    if seller is None or not verify_password(payload.password, seller.password_hash):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    request.session["seller_id"] = str(seller.id)
    return _me(seller)


@app.post("/auth/logout")
def logout(request: Request) -> dict[str, str]:
    request.session.clear()
    return {"status": "logged_out"}


@app.get("/auth/me", response_model=MeOut)
def me(seller: Seller = Depends(current_seller)) -> MeOut:
    return _me(seller)


# --- данные кабинета (всё в контексте текущего селлера) ---


@app.get("/api/fines", response_model=list[FineOut])
def list_fines(
    status: str | None = Query(None),
    category: str | None = Query(None),
    seller: Seller = Depends(current_seller),
    repo: Repository = Depends(get_repo),
) -> list[FineOut]:
    fines = repo.list_fines(seller.id, status=status, category=category)
    return [_to_fine_out(f) for f in fines]


@app.get("/api/summary", response_model=SummaryOut)
def summary(
    seller: Seller = Depends(current_seller), repo: Repository = Depends(get_repo)
) -> SummaryOut:
    s = repo.summary(seller.id)
    return SummaryOut(
        recoverable_total=s.recoverable_total, recovered_total=s.recovered_total,
        fines_total=s.fines_total, disputable_total=s.disputable_total,
    )


@app.get("/api/fines/{fine_id}", response_model=FineCardOut)
def fine_card(
    fine_id: uuid.UUID, seller: Seller = Depends(current_seller),
    repo: Repository = Depends(get_repo),
) -> FineCardOut:
    fine = _own_fine_or_404(repo, seller, fine_id)
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
    fine_id: uuid.UUID, payload: EventIn,
    seller: Seller = Depends(current_seller), repo: Repository = Depends(get_repo),
) -> DisputeEventOut:
    _own_fine_or_404(repo, seller, fine_id)
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
    fine_id: uuid.UUID, seller: Seller = Depends(current_seller),
    repo: Repository = Depends(get_repo),
) -> DisputeDraftOut:
    _own_fine_or_404(repo, seller, fine_id)
    try:
        draft = DisputeGenerator(repo).generate_for_fine(fine_id, seller.name)
    except DraftError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return DisputeDraftOut(
        body_md=draft.body_md, evidence_checklist=draft.evidence_checklist,
        generated_at=draft.generated_at,
    )


# --- управление токеном и запуск проверки ---


@app.post("/api/token", response_model=MeOut)
def set_token(
    payload: TokenIn, seller: Seller = Depends(current_seller),
    repo: Repository = Depends(get_repo),
) -> MeOut:
    raw = payload.wb_token.strip()
    if not raw:
        raise HTTPException(status_code=422, detail="Пустой токен")
    repo.update_token(seller.id, wb_token_enc=_encrypt_token(raw), token_scopes=["read"])
    updated = repo.get_seller(seller.id)
    return _me(updated or seller)


@app.post("/api/ingest/run", response_model=IngestRunOut)
def run_ingest(
    seller: Seller = Depends(current_seller), repo: Repository = Depends(get_repo)
) -> IngestRunOut:
    """Запустить выгрузку+классификацию своего кабинета сейчас."""
    settings = get_settings()
    config = get_app_config()
    mock = settings.wb_client_mode.lower() == "mock"
    if not mock and not seller.has_token:
        raise HTTPException(status_code=400, detail="Сначала добавьте WB-токен")

    client = build_report_client(config.wb, settings=settings)
    cipher = _StubCipher() if mock else TokenCipher()
    ing = IngestionWorker(repo, client, config=config, cipher=cipher).run_for_seller(seller)
    clf = Classifier(repo, config=config).classify_seller(seller.id)
    expired = repo.expire_overdue(date.today())
    return IngestRunOut(
        rows_seen=ing.rows_seen, rows_new=ing.rows_new, fines_new=clf.fines_new,
        by_category=clf.by_category, expired=expired, status=ing.status,
    )
