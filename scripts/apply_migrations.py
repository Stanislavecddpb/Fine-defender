"""Применение SQL-миграций к Postgres (с ожиданием доступности БД).

Идемпотентно: миграции написаны через IF NOT EXISTS. Применяет все файлы
migrations/*.sql в лексикографическом порядке.

Запуск:
  python scripts/apply_migrations.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from fine_defender.config import get_settings  # noqa: E402
from fine_defender.logging_utils import configure_logging  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def wait_for_db(engine, *, attempts: int = 30, delay: float = 2.0) -> None:
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            print(f"БД доступна (попытка {i})")
            return
        except OperationalError as exc:
            last = exc
            print(f"Ожидание БД... ({i}/{attempts})")
            time.sleep(delay)
    raise RuntimeError(f"БД недоступна после {attempts} попыток: {last}")


def main() -> int:
    configure_logging()
    engine = create_engine(get_settings().database_url, future=True)
    wait_for_db(engine)

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("Миграции не найдены")
        return 0
    for f in files:
        sql = f.read_text(encoding="utf-8")
        with engine.begin() as conn:
            conn.exec_driver_sql(sql)
        print(f"Применена миграция: {f.name}")
    print("Миграции применены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
