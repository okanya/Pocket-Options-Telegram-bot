import logging

from app.config import Settings
from app.logging_config import SecretMaskingFilter


def test_secret_masking_filter_masks_known_secrets() -> None:
    settings = Settings(_env_file=None, telegram_bot_token="token-secret")
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "token-secret", (), None)

    SecretMaskingFilter([settings.telegram_bot_token]).filter(record)

    assert "token-secret" not in record.msg
    assert record.msg == "***"
