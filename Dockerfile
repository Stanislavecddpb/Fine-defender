# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUTF8=1

WORKDIR /app

# Сначала зависимости (кэш слоёв)
COPY pyproject.toml ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# Остальной проект (конфиг, миграции, скрипты, сэмплы)
COPY config ./config
COPY migrations ./migrations
COPY scripts ./scripts
COPY samples ./samples
COPY tests ./tests
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
