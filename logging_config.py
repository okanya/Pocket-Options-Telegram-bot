from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from app.config import Settings

MASK = "***"


class SecretMaskingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str | None]) -> None:
        super().__init__()
        self._secrets = [secret for secret in secrets if secret]

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._mask(record.msg)
        if record.args:
            record.args = tuple(self._mask(arg) for arg in record.args)
        return True

    def _mask(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        masked = value
        for secret in self._secrets:
            masked = masked.replace(secret, MASK)
        return masked


def setup_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    secret_filter = SecretMaskingFilter(
        secrets=[
            settings.telegram_bot_token,
        ]
    )
    root = logging.getLogger()
    root.addFilter(secret_filter)
    for handler in root.handlers:
        handler.addFilter(secret_filter)
