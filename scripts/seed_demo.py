"""Засев демо-селлера для боевого стека в mock-режиме (без реального токена).

Идемпотентно: если в БД уже есть хотя бы один селлер — ничего не делает.
Токен — плейсхолдер, шифруется тем же ключом (mock-клиент его игнорирует).

Запуск:
  python scripts/seed_demo.py
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from fine_defender.config import get_settings  # noqa: E402
from fine_defender.crypto import TokenCipher  # noqa: E402
from fine_defender.db.models import Seller  # noqa: E402
from fine_defender.logging_utils import configure_logging  # noqa: E402
from fine_defender.security import hash_password  # noqa: E402


def main() -> int:
    configure_logging()
    settings = get_settings()
    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as s:
        existing = s.scalar(select(Seller).limit(1))
        if existing is not None:
            print(f"Селлеры уже есть (id={existing.id}) — засев пропущен.")
            return 0
        enc = TokenCipher().encrypt("DEMO-PLACEHOLDER-TOKEN")
        email = "demo@fine-defender.ru"
        password = "demo12345"
        seller = Seller(
            id=uuid.uuid4(), name="ООО Ромашка (демо)", wb_token_enc=enc,
            token_scopes=["read"], created_at=datetime.now(timezone.utc), is_active=True,
            email=email, password_hash=hash_password(password),
        )
        s.add(seller)
        s.commit()
        print(f"Демо-селлер заведён: id={seller.id}")
        print(f"Вход в кабинет: {email} / {password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
