"""Фоновый воркер: периодическая выгрузка + классификация на Postgres.

Цикл: при старте и далее каждые WORKER_INTERVAL_SECONDS секунд прогоняет
M1 (выгрузка в raw_transactions) и M2 (классификация в fines) по всем активным
селлерам. Каждый прогон фиксируется в ingestion_runs (наблюдаемость).

Запуск:
  python scripts/worker.py            # бесконечный цикл
  python scripts/worker.py --once     # один прогон и выход
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fine_defender.classifier import Classifier  # noqa: E402
from fine_defender.config import get_app_config, get_settings  # noqa: E402
from fine_defender.db.postgres_repository import PostgresRepository  # noqa: E402
from fine_defender.ingestion.worker import IngestionWorker  # noqa: E402
from fine_defender.logging_utils import configure_logging  # noqa: E402
from fine_defender.wb.client import build_report_client  # noqa: E402

logger = logging.getLogger("fine_defender.worker")


def run_cycle() -> None:
    settings = get_settings()
    config = get_app_config()
    repo = PostgresRepository(settings.database_url)
    client = build_report_client(config.wb, settings=settings)

    ingest = IngestionWorker(repo, client, config=config).run_all()
    for r in ingest:
        logger.info("ingest: %s", r)
    classify = Classifier(repo, config=config).classify_all()
    for r in classify:
        logger.info("classify: %s", r)

    expired = repo.expire_overdue(date.today())
    logger.info("expire: %d штрафов с пропущенным дедлайном → expired", expired)


def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description="Фоновый воркер выгрузки+классификации")
    parser.add_argument("--once", action="store_true", help="Один прогон и выход")
    args = parser.parse_args()

    interval = get_settings().worker_interval_seconds
    while True:
        try:
            run_cycle()
        except Exception:  # воркер не должен падать насмерть из-за одного цикла
            logger.exception("Ошибка в цикле воркера")
        if args.once:
            return 0
        logger.info("Следующий прогон через %d сек", interval)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
