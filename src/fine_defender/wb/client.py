"""HTTP-клиент WB statistics API: пагинация по rrdid, ретраи с backoff.

Протокол ReportClient позволяет подменять реализацию моком в тестах и на этапе
без реального токена (выбор пользователя на MVP — работаем на моках).

Надёжность (раздел 9 ТЗ): эндпоинт исторически нестабилен — ретраи с
экспоненциальным backoff на 429/5xx и сетевых ошибках. Исчерпание попыток
поднимает WbApiError, который воркер фиксирует как failed-прогон (dead-letter).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import Settings, WbConfig, get_settings
from ..logging_utils import mask_token

logger = logging.getLogger(__name__)


class WbApiError(RuntimeError):
    """Невосстановимая ошибка обращения к WB API (после исчерпания ретраев)."""


class ReportClient(Protocol):
    """Источник строк отчёта о реализации. Реализации: HTTP и Mock."""

    def fetch_rows(self, token: str, date_from: date) -> Iterator[dict[str, Any]]:
        ...


# Ошибки, на которых имеет смысл ретраиться.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class _RetryableHttp(Exception):
    pass


class WbReportClient:
    """Боевой клиент reportDetailByPeriod."""

    def __init__(self, config: WbConfig, *, http_client: httpx.Client | None = None) -> None:
        self._config = config
        self._client = http_client or httpx.Client(
            base_url=config.base_url, timeout=config.request_timeout_seconds
        )

    def fetch_rows(self, token: str, date_from: date) -> Iterator[dict[str, Any]]:
        """Выгрузить все строки отчёта с date_from, постранично по rrdid.

        WB возвращает до page_limit строк; курсор — rrdid последней строки
        предыдущего ответа. Останавливаемся, когда страница пуста.
        """
        logger.info(
            "WB fetch_rows: date_from=%s token=%s", date_from.isoformat(), mask_token(token)
        )
        rrdid = 0
        total = 0
        while True:
            page = self._fetch_page(token, date_from, rrdid)
            if not page:
                break
            for row in page:
                yield row
            total += len(page)
            last = page[-1]
            next_rrdid = last.get("rrd_id") or last.get("rrdId")
            # Защита от зацикливания, если курсор не сдвинулся.
            if next_rrdid in (None, rrdid):
                break
            rrdid = next_rrdid
            if len(page) < self._config.page_limit:
                break
        logger.info("WB fetch_rows завершён: получено строк=%d", total)

    @retry(
        retry=retry_if_exception_type((_RetryableHttp, httpx.TransportError)),
        wait=wait_exponential(multiplier=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _fetch_page(self, token: str, date_from: date, rrdid: int) -> list[dict[str, Any]]:
        try:
            resp = self._client.get(
                self._config.report_endpoint,
                headers={"Authorization": token},
                params={
                    "dateFrom": date_from.isoformat(),
                    "rrdid": rrdid,
                    "limit": self._config.page_limit,
                },
            )
        except httpx.TransportError as exc:
            logger.warning("WB сетевая ошибка, ретрай: %s", type(exc).__name__)
            raise
        if resp.status_code in _RETRYABLE_STATUS:
            logger.warning("WB вернул %s, ретрай", resp.status_code)
            raise _RetryableHttp(f"status={resp.status_code}")
        if resp.status_code >= 400:
            raise WbApiError(f"WB API status={resp.status_code}")
        data = resp.json()
        # Терпимы к форме: список на верхнем уровне или вложенный.
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "report", "result"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []


class MockReportClient:
    """Мок: отдаёт строки из заранее заданного списка (фикстуры).

    Используется на MVP до подключения реального токена и в тестах.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetch_rows(self, token: str, date_from: date) -> Iterator[dict[str, Any]]:
        yield from self._rows


def build_report_client(
    wb_config: WbConfig, *, settings: Settings | None = None
) -> ReportClient:
    """Фабрика клиента по WB_CLIENT_MODE: live (реальный API) или mock (фикстура).

    mock-режим позволяет запускать боевой стек (Postgres, воркер, API) без
    реального токена — для демонстрации и приёмки.
    """
    settings = settings or get_settings()
    if settings.wb_client_mode.lower() == "mock":
        rows = json.loads(Path(settings.sample_report_path).read_text(encoding="utf-8"))
        logger.info("WB client: MOCK (%d строк из %s)", len(rows), settings.sample_report_path)
        return MockReportClient(rows)
    logger.info("WB client: LIVE (%s)", wb_config.base_url)
    return WbReportClient(wb_config)
