#!/usr/bin/env bash
# Единый entrypoint. Роль передаётся аргументом:
#   migrate    — применить миграции и выйти
#   seed-demo  — засеять демо-селлера (идемпотентно) и выйти
#   worker     — фоновый воркер выгрузки+классификации
#   api        — REST API (uvicorn)
set -euo pipefail

ROLE="${1:-api}"

case "$ROLE" in
  migrate)
    exec python scripts/apply_migrations.py
    ;;
  seed-demo)
    exec python scripts/seed_demo.py
    ;;
  worker)
    # воркер ждёт, пока миграции применит api/migrate-сервис
    python scripts/apply_migrations.py
    if [ "${DEMO_SEED:-false}" = "true" ]; then python scripts/seed_demo.py; fi
    exec python scripts/worker.py
    ;;
  api)
    python scripts/apply_migrations.py
    if [ "${DEMO_SEED:-false}" = "true" ]; then python scripts/seed_demo.py; fi
    exec uvicorn fine_defender.api.main:app \
        --host 0.0.0.0 --port 8000 \
        --workers "${UVICORN_WORKERS:-2}"
    ;;
  *)
    echo "Неизвестная роль: $ROLE (ожидается: migrate|seed-demo|worker|api)" >&2
    exit 1
    ;;
esac
