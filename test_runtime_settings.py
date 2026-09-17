from decimal import Decimal

from app.config import Settings
from app.runtime_settings import apply_runtime_overrides


def test_apply_runtime_overrides_updates_known_settings() -> None:
    settings = apply_runtime_overrides(
        Settings(_env_file=None),
        {
            "active_strategy": {"value": "bollinger_mean_reversion"},
            "active_assets": {"value": ["EURUSD_otc", "GBPUSD_otc"]},
            "trade_amount": {"value": "2.5"},
            "expiration_mode": {"value": "auto"},
            "expiration_seconds": {"value": "120"},
            "signal_only": {"value": "false"},
            "trading_enabled": {"value": "true"},
            "paper_trading": {"value": "true"},
            "qt_account_mode": {"value": "real"},
            "real_trading_unlocked": {"value": "true"},
            "real_trading_admin_confirmed": {"value": "true"},
            "min_payout": {"value": "0.75"},
        },
    )

    assert settings.active_strategy == "bollinger_mean_reversion"
    assert settings.active_assets == ["EURUSD_otc", "GBPUSD_otc"]
    assert settings.trade_amount == Decimal("2.5")
    assert settings.expiration_mode == "auto"
    assert settings.expiration_seconds == 120
    assert settings.signal_only is False
    assert settings.trading_enabled is True
    assert settings.paper_trading is True
    assert settings.qt_account_mode == "real"
    assert settings.real_trading_unlocked is True
    assert settings.real_trading_admin_confirmed is True
    assert settings.min_payout == Decimal("0.75")


def test_apply_runtime_overrides_accepts_all_strategy_mode() -> None:
    settings = apply_runtime_overrides(Settings(_env_file=None), {"active_strategy": {"value": "all"}})

    assert settings.active_strategy == "all"


def test_apply_runtime_overrides_ignores_invalid_values() -> None:
    base = Settings(_env_file=None)

    settings = apply_runtime_overrides(
        base,
        {
            "active_strategy": {"value": "unknown"},
            "active_assets": {"value": []},
            "trade_amount": {"value": "-1"},
            "expiration_mode": {"value": "random"},
            "expiration_seconds": {"value": "0"},
            "signal_only": {"value": "maybe"},
            "paper_trading": {"value": "maybe"},
            "real_trading_unlocked": {"value": "maybe"},
            "real_trading_admin_confirmed": {"value": "maybe"},
            "qt_account_mode": {"value": "mt5"},
        },
    )

    assert settings.active_strategy == base.active_strategy
    assert settings.active_assets == base.active_assets
    assert settings.trade_amount == base.trade_amount
    assert settings.expiration_mode == base.expiration_mode
    assert settings.expiration_seconds == base.expiration_seconds
    assert settings.signal_only == base.signal_only
    assert settings.paper_trading == base.paper_trading
    assert settings.real_trading_unlocked == base.real_trading_unlocked
    assert settings.real_trading_admin_confirmed == base.real_trading_admin_confirmed
    assert settings.qt_account_mode == base.qt_account_mode


def test_apply_runtime_overrides_keeps_safe_defaults_when_empty() -> None:
    settings = apply_runtime_overrides(Settings(_env_file=None), {})

    assert settings.signal_only is True
    assert settings.trading_enabled is False
    assert settings.paper_trading is False


def test_apply_runtime_overrides_parses_csv_assets() -> None:
    settings = apply_runtime_overrides(
        Settings(_env_file=None),
        {"active_assets": {"value": "EURUSD_otc, GBPUSD_otc"}},
    )

    assert settings.active_assets == ["EURUSD_otc", "GBPUSD_otc"]
