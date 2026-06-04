"""Завести селлера вручную (публичной регистрации на MVP нет).

Токен WB шифруется перед записью в БД и НЕ логируется. Токен читается из
переменной окружения WB_TOKEN (не из аргумента командной строки — чтобы не
осесть в истории шелла).

Запуск:
  WB_TOKEN="<read-only токен категории Финансы>" \
  python scripts/seed_seller.py --name "ООО Ромашка"
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Позволяем запускать как скрипт без установки пакета.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fine_defender.config import get_settings  # noqa: E402
from fine_defender.crypto import TokenCipher  # noqa: E402
from fine_defender.db.models import Seller  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Завести селлера с зашифрованным WB-токеном")
    parser.add_argument("--name", required=True, help="Название селлера")
    parser.add_argument(
        "--scopes", default="read", help="Скоупы токена через запятую (только read на MVP)"
    )
    parser.add_argument("--email", help="Логин для входа в кабинет (опционально)")
    args = parser.parse_args()

    token = os.environ.get("WB_TOKEN")
    if not token:
        print("Ошибка: задайте WB_TOKEN через переменную окружения.", file=sys.stderr)
        return 1

    # Пароль для входа (опционально) — через env, чтобы не светить в истории шелла.
    password = os.environ.get("SELLER_PASSWORD")
    pwd_hash = None
    if args.email:
        if not password:
            print("Ошибка: для --email задайте SELLER_PASSWORD через env.", file=sys.stderr)
            return 1
        from fine_defender.security import hash_password
        pwd_hash = hash_password(password)

    settings = get_settings()
    cipher = TokenCipher()
    enc = cipher.encrypt(token)

    engine = create_engine(settings.database_url, future=True)
    with Session(engine) as s:
        seller = Seller(
            id=uuid.uuid4(),
            name=args.name,
            email=(args.email.strip().lower() if args.email else None),
            password_hash=pwd_hash,
            wb_token_enc=enc,
            token_scopes=[x.strip() for x in args.scopes.split(",") if x.strip()],
            created_at=datetime.now(timezone.utc),
            is_active=True,
        )
        s.add(seller)
        s.commit()
        login = f" login={seller.email}" if seller.email else ""
        print(f"Селлер заведён: id={seller.id} name={seller.name}{login}")  # токен/пароль не печатаем
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
