"""Логирование с редакцией секретов.

Токен WB НИКОГДА не должен попасть в логи (раздел 9 ТЗ). Здесь — фильтр,
который маскирует значения заголовка Authorization и явно помеченные секреты,
плюс хелпер безопасного укорачивания токена для диагностики.
"""

from __future__ import annotations

import logging
import re

from .config import get_settings

# Маскируем содержимое Authorization-заголовков и Bearer/JWT-подобных строк.
_AUTH_PATTERN = re.compile(
    r"(authorization\s*[:=]\s*)(\S+)",
    re.IGNORECASE,
)
# Длинные «токеноподобные» строки (eyJ... JWT и длинные хвосты).
_TOKENISH = re.compile(r"\b(eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)\b")


def redact(text: str) -> str:
    text = _AUTH_PATTERN.sub(r"\1***REDACTED***", text)
    text = _TOKENISH.sub("***REDACTED***", text)
    return text


def mask_token(token: str) -> str:
    """Безопасный «отпечаток» токена для диагностики: длина + последние 4 символа."""
    if not token:
        return "<empty>"
    tail = token[-4:] if len(token) >= 4 else ""
    return f"len={len(token)} …{tail}"


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = tuple(
                redact(a) if isinstance(a, str) else a for a in record.args
            )
        return True


def configure_logging() -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.addFilter(_RedactingFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s :: %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
