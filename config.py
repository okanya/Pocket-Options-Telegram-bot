from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return value
    return list(value)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://pocket_bot:change_me@localhost:5432/pocket_bot"

    telegram_bot_token: str | None = None
    telegram_admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    telegram_notify_chat_id: int | None = None

    signal_only: bool = True
    trading_enabled: bool = False
    paper_trading: bool = False
    qt_account_mode: Literal["demo", "real"] = "demo"
    real_trading_unlocked: bool = False
    real_trading_admin_confirmed: bool = False
    active_assets: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "AUDUSD_otc", "EURJPY_otc"]
    )
    active_strategy: str = "trend_pullback"
    trade_amount: Decimal = Decimal("1")
    expiration_mode: Literal["fixed", "auto"] = "fixed"
    expiration_seconds: int = 60
    timeframe_seconds: int = 60

    max_trades_total_per_day: int = 5
    max_trades_per_asset_per_day: int = 1
    max_open_trades_total: int = 1
    max_open_trades_per_asset: int = 1
    max_losses_per_day: int = 2
    max_consecutive_losses: int = 2
    daily_stop_loss: Decimal = Decimal("10")
    daily_take_profit: Decimal = Decimal("20")
    min_payout: Decimal = Decimal("0.7")
    one_trade_per_candle: bool = True
    one_trade_per_asset: bool = True
    loss_cooldown_seconds: int = 900
    asset_cooldown_seconds: int = 300

    @field_validator("telegram_admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: Any) -> list[int]:
        return [int(item) for item in _split_csv(value)]

    @field_validator("active_assets", mode="before")
    @classmethod
    def parse_active_assets(cls, value: Any) -> list[str]:
        return _split_csv(value)

    @field_validator("telegram_bot_token", "telegram_notify_chat_id", mode="before")
    @classmethod
    def parse_optional_empty(cls, value: Any) -> Any:
        if value == "":
            return None
        return value

    @property
    def is_telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    @property
    def safe_mode_name(self) -> str:
        if self.signal_only:
            return "signal_only"
        if self.paper_trading:
            return "paper"
        return self.qt_account_mode if self.trading_enabled else "demo"


@lru_cache
def get_settings() -> Settings:
    return Settings()
