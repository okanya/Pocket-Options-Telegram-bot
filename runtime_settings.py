from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.repositories import SettingsRepository
from app.trading.strategies import validate_strategy_name

logger = logging.getLogger(__name__)


RUNTIME_SETTING_KEYS = {
    "signal_only",
    "trading_enabled",
    "paper_trading",
    "qt_account_mode",
    "real_trading_unlocked",
    "real_trading_admin_confirmed",
    "active_assets",
    "active_strategy",
    "trade_amount",
    "expiration_mode",
    "expiration_seconds",
    "timeframe_seconds",
    "max_trades_total_per_day",
    "max_trades_per_asset_per_day",
    "max_open_trades_total",
    "max_open_trades_per_asset",
    "max_losses_per_day",
    "max_consecutive_losses",
    "daily_stop_loss",
    "daily_take_profit",
    "min_payout",
    "one_trade_per_candle",
    "one_trade_per_asset",
    "loss_cooldown_seconds",
    "asset_cooldown_seconds",
}

DECIMAL_KEYS = {"trade_amount", "daily_stop_loss", "daily_take_profit", "min_payout"}
POSITIVE_INT_KEYS = {
    "expiration_seconds",
    "timeframe_seconds",
    "max_trades_total_per_day",
    "max_trades_per_asset_per_day",
    "max_open_trades_total",
    "max_open_trades_per_asset",
    "max_losses_per_day",
    "max_consecutive_losses",
    "loss_cooldown_seconds",
    "asset_cooldown_seconds",
}


class RuntimeSettingsService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def load(self, base_settings: Settings) -> Settings:
        async with self.session_factory() as session:
            rows = await SettingsRepository(session).get_many(RUNTIME_SETTING_KEYS)
        return apply_runtime_overrides(base_settings, {key: row.value for key, row in rows.items()})


def apply_runtime_overrides(base_settings: Settings, raw_settings: dict[str, dict[str, Any]]) -> Settings:
    overrides: dict[str, Any] = {}
    for key in RUNTIME_SETTING_KEYS:
        if key not in raw_settings:
            continue
        try:
            overrides[key] = _parse_runtime_value(key, raw_settings[key])
        except ValueError as exc:
            logger.warning("Ignoring invalid runtime setting %s: %s", key, exc)

    if not overrides:
        return base_settings

    try:
        return base_settings.model_copy(update=overrides)
    except ValidationError as exc:
        logger.warning("Ignoring runtime settings because validation failed: %s", exc)
        return base_settings


def _parse_runtime_value(key: str, raw: dict[str, Any]) -> Any:
    if not isinstance(raw, dict) or "value" not in raw:
        raise ValueError("expected object with value")

    value = raw["value"]
    if key == "active_strategy":
        if not isinstance(value, str) or not value:
            raise ValueError("strategy must be a non-empty string")
        validate_strategy_name(value)
        return value

    if key == "active_assets":
        assets = _parse_assets(value)
        if not assets:
            raise ValueError("active_assets must not be empty")
        return assets

    if key == "qt_account_mode":
        if value not in {"demo", "real"}:
            raise ValueError("qt_account_mode must be demo or real")
        return value

    if key == "expiration_mode":
        if value not in {"fixed", "auto"}:
            raise ValueError("expiration_mode must be fixed or auto")
        return value

    if key in DECIMAL_KEYS:
        decimal_value = _parse_decimal(value)
        if decimal_value <= 0:
            raise ValueError("decimal value must be positive")
        return decimal_value

    if key in POSITIVE_INT_KEYS:
        int_value = _parse_int(value)
        if int_value <= 0:
            raise ValueError("integer value must be positive")
        return int_value

    if key in {
        "signal_only",
        "trading_enabled",
        "paper_trading",
        "real_trading_unlocked",
        "real_trading_admin_confirmed",
        "one_trade_per_candle",
        "one_trade_per_asset",
    }:
        return _parse_bool(value)

    raise ValueError("unsupported key")


def _parse_assets(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        return [item.strip() for item in value]
    raise ValueError("expected comma-separated string or list of strings")


def _parse_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ValueError("expected decimal-compatible value") from exc


def _parse_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("expected integer")
    try:
        return int(value)
    except Exception as exc:
        raise ValueError("expected integer") from exc


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise ValueError("expected boolean")
