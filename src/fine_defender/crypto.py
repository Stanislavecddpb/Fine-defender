"""Шифрование токенов WB (envelope encryption).

На MVP используем симметричный Fernet с ключом из секрет-менеджера/env.
В проде TOKEN_ENCRYPTION_KEY — это data-key, выдаваемый KMS (Yandex Cloud KMS /
VK Cloud / Selectel); сам мастер-ключ из приложения недоступен. Интерфейс ниже
не меняется при переходе на KMS — меняется только источник ключа.

Требование безопасности (раздел 9 ТЗ): токен хранится только зашифрованным,
никогда не пишется в логи, scope — только read.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


class TokenCipher:
    """Шифрование/расшифровка WB-токена для столбца sellers.wb_token_enc (BYTEA)."""

    def __init__(self, key: str | None = None) -> None:
        key = key if key is not None else get_settings().token_encryption_key
        if not key:
            raise RuntimeError(
                "TOKEN_ENCRYPTION_KEY не задан. Сгенерировать: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, token: str) -> bytes:
        return self._fernet.encrypt(token.encode("utf-8"))

    def decrypt(self, blob: bytes) -> str:
        try:
            return self._fernet.decrypt(blob).decode("utf-8")
        except InvalidToken as exc:  # noqa: F841 — намеренно без деталей в сообщении
            raise RuntimeError("Не удалось расшифровать токен: неверный ключ или данные") from None
