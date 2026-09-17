from app.config import Settings


def test_safe_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.signal_only is True
    assert settings.trading_enabled is False
    assert settings.paper_trading is False
    assert settings.qt_account_mode == "demo"
    assert settings.real_trading_unlocked is False
    assert settings.real_trading_admin_confirmed is False


def test_csv_parsing() -> None:
    settings = Settings(_env_file=None, telegram_admin_ids="1, 2", active_assets="EURUSD_otc, GBPUSD_otc")

    assert settings.telegram_admin_ids == [1, 2]
    assert settings.active_assets == ["EURUSD_otc", "GBPUSD_otc"]


def test_csv_parsing_from_env(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1,2")
    monkeypatch.setenv("ACTIVE_ASSETS", "EURUSD_otc,GBPUSD_otc")

    settings = Settings(_env_file=None)

    assert settings.telegram_admin_ids == [1, 2]
    assert settings.active_assets == ["EURUSD_otc", "GBPUSD_otc"]


def test_empty_optional_env_values_parse_as_none() -> None:
    settings = Settings(_env_file=None, telegram_bot_token="", telegram_notify_chat_id="")

    assert settings.telegram_bot_token is None
    assert settings.telegram_notify_chat_id is None
