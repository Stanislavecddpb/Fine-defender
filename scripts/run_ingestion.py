"""Запуск выгрузки (M1) вручную или по cron.

Два режима:
  --mock           — прогон на фикстуре tests/fixtures/report_detail_sample.json
                     (без БД и без реального токена). Печатает сырые транзакции —
                     это и есть «показать сырьё до классификатора» из ТЗ.
  (по умолчанию)   — боевой: Postgres + реальный WB API по токенам из sellers.

Примеры:
  python scripts/run_ingestion.py --mock
  python scripts/run_ingestion.py --date-from 2026-05-01
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fine_defender.config import get_app_config, get_settings  # noqa: E402
from fine_defender.ingestion.worker import IngestionWorker  # noqa: E402
from fine_defender.logging_utils import configure_logging  # noqa: E402
from fine_defender.repository import InMemoryRepository, Seller  # noqa: E402
from fine_defender.wb.client import MockReportClient, WbReportClient  # noqa: E402

FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "tests", "fixtures", "report_detail_sample.json"
)


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def run_mock(date_from: date | None) -> int:
    """Прогон на фикстуре: показать сырьё + проверить идемпотентность."""
    with open(FIXTURE, encoding="utf-8") as f:
        rows = json.load(f)

    repo = InMemoryRepository(
        sellers=[Seller(id=uuid.uuid4(), name="MOCK", wb_token_enc=b"", token_scopes=["read"])]
    )
    # Мок не расшифровывает токен — передаём cipher-заглушку через monkeypatch токена.
    worker = IngestionWorker(repo, MockReportClient(rows), config=get_app_config(), cipher=_NoopCipher())

    print(f"=== Сырые транзакции из фикстуры ({len(rows)} строк) ===")
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))

    print("\n=== Прогон выгрузки ===")
    res = worker.run_all(date_from)
    for r in res:
        print(r)

    print("\n=== Повторный прогон (проверка идемпотентности) ===")
    res2 = worker.run_all(date_from)
    for r in res2:
        print(r)
    assert all(r.rows_new == 0 for r in res2), "Повторная выгрузка не должна плодить новые строки!"
    print("OK: повторная выгрузка не создала дублей.")
    return 0


class _NoopCipher:
    def decrypt(self, blob: bytes) -> str:  # noqa: D401
        return "mock-token"


def run_live(date_from: date | None) -> int:
    from fine_defender.classifier import Classifier
    from fine_defender.db.postgres_repository import PostgresRepository

    from fine_defender.wb.client import build_report_client

    settings = get_settings()
    config = get_app_config()
    repo = PostgresRepository(settings.database_url)
    client = build_report_client(config.wb, settings=settings)

    print("=== M1: выгрузка ===")
    for r in IngestionWorker(repo, client, config=config).run_all(date_from):
        print(f"  {r}")

    print("=== M2: классификация ===")
    for r in Classifier(repo, config=config).classify_all():
        print(f"  {r}")
    return 0


def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description="Запуск выгрузки отчёта WB (M1)")
    parser.add_argument("--mock", action="store_true", help="Прогон на фикстуре без БД/токена")
    parser.add_argument("--date-from", help="Дата начала периода YYYY-MM-DD")
    args = parser.parse_args()

    date_from = _parse_date(args.date_from)
    return run_mock(date_from) if args.mock else run_live(date_from)


if __name__ == "__main__":
    raise SystemExit(main())
