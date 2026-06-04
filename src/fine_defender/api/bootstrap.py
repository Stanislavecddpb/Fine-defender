"""Сборка репозитория для API.

Бэкенд выбирается переменной окружения APP_BACKEND:
  memory   (по умолчанию на MVP) — InMemoryRepository, при старте засевается
           демо-данными из фикстуры (ingest + classify), чтобы дашборд сразу
           показывал реальные вычисленные штрафы;
  postgres — боевой PostgresRepository (без засева).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from ..classifier import Classifier
from ..config import get_app_config, get_settings
from ..repository import InMemoryRepository, RawTxnRecord, Repository, Seller
from ..security import hash_password
from ..wb.adapter import normalize_row

logger = logging.getLogger(__name__)

_DEFAULT_SAMPLE = Path("tests/fixtures/report_detail_sample.json")

# Демо-логин для memory-бэкенда (кабинет можно открыть без регистрации).
DEMO_EMAIL = "demo@fine-defender.ru"
DEMO_PASSWORD = "demo12345"


def _seed_memory() -> InMemoryRepository:
    seller = Seller(
        id=uuid.uuid4(), name="ООО Ромашка (демо)", wb_token_enc=b"", token_scopes=["read"],
        email=DEMO_EMAIL, password_hash=hash_password(DEMO_PASSWORD),
    )
    repo = InMemoryRepository(sellers=[seller])
    logger.info("Демо-вход: %s / %s", DEMO_EMAIL, DEMO_PASSWORD)

    sample_path = Path(os.environ.get("SAMPLE_REPORT_PATH", _DEFAULT_SAMPLE))
    if not sample_path.exists():
        logger.warning("Демо-фикстура не найдена: %s — дашборд будет пустым", sample_path)
        return repo

    rows = json.loads(sample_path.read_text(encoding="utf-8"))
    records: list[RawTxnRecord] = []
    for raw in rows:
        norm = normalize_row(raw)
        if norm is None:
            continue
        records.append(RawTxnRecord(wb_txn_key=norm.txn_key, raw_json=raw))
    repo.upsert_raw_transactions(seller.id, records)
    Classifier(repo, config=get_app_config()).classify_all()
    logger.info(
        "Демо-данные загружены: сырых=%d штрафов=%d", len(repo.raw), len(repo.fines)
    )
    return repo


def build_repository() -> Repository:
    settings = get_settings()
    backend = os.environ.get("APP_BACKEND", settings.app_backend).lower()
    if backend == "postgres":
        from ..db.postgres_repository import PostgresRepository

        logger.info("API backend: postgres")
        return PostgresRepository(settings.database_url)
    logger.info("API backend: memory (демо)")
    return _seed_memory()
