from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.telegram.handlers import (
    CONFIRMATION_TTL_SECONDS,
    PendingConfirmation,
    _extract_command_argument,
    _format_available_strategies,
    _format_available_assets,
    _format_available_assets_chunks,
    _format_confirmation_request,
    _format_status,
    _is_confirmation_expired,
    _looks_like_pocket_option_auth_ssid,
    _looks_like_pocket_option_cookie_ssid,
    _looks_like_pocket_option_ssid,
    _mask_secret,
    _normalize_pocket_option_ssid,
    _pending_confirmation_key,
    _pending_from_setting_value,
    _pending_to_setting_value,
    _pop_pending_confirmation,
    _parse_assets_argument,
    _parse_clear_chat_limit,
    _parse_positive_decimal_argument,
    _parse_positive_int_argument,
    _slow_signals_preset_values,
)
from app.config import Settings
from app.trading.engine import EngineStatus
from app.trading.pocket_service import PocketConnectionStatus


def test_extract_command_argument() -> None:
    assert _extract_command_argument('/set_ssid 42["auth",abc]') == '42["auth",abc]'
    assert _extract_command_argument("/set_ssid") == ""
    assert _extract_command_argument(None) == ""


def test_mask_secret() -> None:
    assert _mask_secret(None) == "not set"
    assert _mask_secret("short") == "***"
    assert _mask_secret('42["auth",abcdef]') == '42["...def]'


def test_looks_like_pocket_option_auth_ssid() -> None:
    assert _looks_like_pocket_option_auth_ssid(
        '42["auth",{"session":"abc","isDemo":1,"uid":123,"platform":2}]'
    )
    assert not _looks_like_pocket_option_auth_ssid("1778092567140")
    assert not _looks_like_pocket_option_auth_ssid('42["auth",1778092567140]')
    assert not _looks_like_pocket_option_auth_ssid('42["subscribe",{"session":"abc","isDemo":1,"uid":123}]')
    assert not _looks_like_pocket_option_auth_ssid('42["auth",{"session":"","isDemo":1,"uid":123}]')


def test_looks_like_pocket_option_cookie_ssid() -> None:
    assert _looks_like_pocket_option_cookie_ssid("12qs4d0fuodn20q6n7agegfcrr")
    assert not _looks_like_pocket_option_cookie_ssid("short")
    assert not _looks_like_pocket_option_cookie_ssid("has spaces inside")
    assert not _looks_like_pocket_option_cookie_ssid(
        '42["auth",{"session":"abc","isDemo":1,"uid":123,"platform":2}]'
    )


def test_looks_like_pocket_option_ssid_accepts_cookie_and_auth_packet() -> None:
    assert _looks_like_pocket_option_ssid("12qs4d0fuodn20q6n7agegfcrr")
    assert _looks_like_pocket_option_ssid('42["auth",{"session":"abc","isDemo":1,"uid":123,"platform":2}]')
    assert not _looks_like_pocket_option_ssid("short")


def test_normalize_pocket_option_ssid_keeps_auth_packet() -> None:
    ssid = '42["auth",{"session":"abc","isDemo":1,"uid":123,"platform":2}]'

    assert _normalize_pocket_option_ssid(ssid) == ssid


def test_normalize_pocket_option_ssid_wraps_cookie_with_existing_auth_metadata() -> None:
    current = (
        '42["auth",{"session":"old","isDemo":1,"uid":131021432,"platform":3,'
        '"isFastHistory":true,"isOptimized":true}]'
    )

    assert _normalize_pocket_option_ssid("ae4194af-4a1e::token", current) == (
        '42["auth",{"session":"ae4194af-4a1e::token","isDemo":1,"uid":131021432,'
        '"platform":3,"isFastHistory":true,"isOptimized":true}]'
    )


def test_format_available_strategies() -> None:
    assert _format_available_strategies() == (
        "Available strategies\n"
        "- all\n"
        "- bollinger_mean_reversion\n"
        "- breakout_retest\n"
        "- trend_pullback"
    )


def test_format_available_assets_marks_active_and_limits_output() -> None:
    text = _format_available_assets(
        {
            "EURUSD_otc": Decimal("0.92"),
            "GBPUSD_otc": Decimal("0.51"),
            "UNKNOWN": None,
        },
        active_assets=["EURUSD_otc"],
        limit=2,
    )

    assert "Available assets: 3" in text
    assert "Active assets: EURUSD_otc" in text
    assert "* EURUSD_otc: 92%" in text
    assert "- GBPUSD_otc: 51%" in text
    assert "...and 1 more" in text


def test_format_available_assets_chunks_returns_all_assets() -> None:
    chunks = _format_available_assets_chunks(
        {
            "EURUSD_otc": Decimal("0.92"),
            "GBPUSD_otc": Decimal("0.51"),
            "AUDUSD_otc": Decimal("0.79"),
        },
        active_assets=["EURUSD_otc"],
        page_size=2,
    )

    assert len(chunks) == 2
    assert "Page: 1/2" in chunks[0]
    assert "Page: 2/2" in chunks[1]
    assert "* EURUSD_otc: 92%" in "\n".join(chunks)
    assert "- AUDUSD_otc: 79%" in "\n".join(chunks)
    assert "- GBPUSD_otc: 51%" in "\n".join(chunks)


def test_format_status_uses_engine_and_db_trade_count() -> None:
    text = _format_status(
        Settings(_env_file=None),
        PocketConnectionStatus(ssid_configured=True, connected=True, mocked=False),
        EngineStatus(running=True, connected=True, mode="signal_only", open_trades=2),
        open_trades_count=3,
    )

    assert "Status: running" in text
    assert "Engine connected: True" in text
    assert "PocketOption SSID: set" in text
    assert "PocketOption: connected" in text
    assert "Open trades: 3" in text


def test_format_status_falls_back_to_engine_open_trades() -> None:
    text = _format_status(
        Settings(_env_file=None),
        PocketConnectionStatus(ssid_configured=False, connected=False, mocked=True),
        EngineStatus(running=False, connected=False, mode="signal_only", open_trades=1),
        open_trades_count=None,
    )

    assert "Status: idle" in text
    assert "PocketOption SSID: not set" in text
    assert "PocketOption: mocked" in text
    assert "Open trades: 1" in text


def test_format_status_uses_engine_connection_for_pocket_option_line() -> None:
    text = _format_status(
        Settings(_env_file=None),
        PocketConnectionStatus(ssid_configured=True, connected=False, mocked=False),
        EngineStatus(running=True, connected=True, mode="signal_only", open_trades=0),
        open_trades_count=0,
    )

    assert "Engine connected: True" in text
    assert "PocketOption: connected" in text


def test_parse_assets_argument() -> None:
    assert _parse_assets_argument("EURUSD_otc, GBPUSD_otc") == ["EURUSD_otc", "GBPUSD_otc"]
    assert _parse_assets_argument("") == []


def test_parse_positive_int_argument() -> None:
    assert _parse_positive_int_argument("60") == 60
    assert _parse_positive_int_argument("0") is None
    assert _parse_positive_int_argument("bad") is None


def test_parse_positive_decimal_argument() -> None:
    assert _parse_positive_decimal_argument("0.75") == Decimal("0.75")
    assert _parse_positive_decimal_argument("0") is None
    assert _parse_positive_decimal_argument("bad") is None


def test_slow_signals_preset_values_are_safe_and_long_horizon() -> None:
    values = _slow_signals_preset_values()

    assert values["signal_only"] is True
    assert values["trading_enabled"] is False
    assert values["paper_trading"] is False
    assert values["active_strategy"] == "all"
    assert values["expiration_mode"] == "auto"
    assert values["timeframe_seconds"] == 300
    assert values["expiration_seconds"] == 1800
    assert values["max_open_trades_total"] == 1


def test_parse_clear_chat_limit() -> None:
    assert _parse_clear_chat_limit("") == 50
    assert _parse_clear_chat_limit("1") == 1
    assert _parse_clear_chat_limit("100") == 100
    assert _parse_clear_chat_limit("101") is None
    assert _parse_clear_chat_limit("bad") is None


def test_format_confirmation_request() -> None:
    pending = PendingConfirmation(
        code="abc123",
        action="qt_real",
        value="real",
        description="Switch QT account mode to real",
        created_at=datetime(2026, 5, 6, tzinfo=UTC),
    )

    assert _format_confirmation_request(pending) == (
        "Confirmation required\n"
        "Action: Switch QT account mode to real\n"
        "Confirm: /confirm abc123\n"
        "Cancel: /cancel\n"
        "Expires in: 5 minutes"
    )


def test_pending_confirmation_setting_roundtrip() -> None:
    pending = PendingConfirmation(
        code="abc123",
        action="enable_paper",
        value="true",
        description="Enable paper",
        created_at=datetime(2026, 5, 6, tzinfo=UTC),
    )

    assert _pending_confirmation_key(100) == "pending_confirmation:100"
    assert _pending_from_setting_value(_pending_to_setting_value(pending)) == pending


def test_pending_confirmation_setting_ignores_invalid_payload() -> None:
    assert _pending_from_setting_value(None) is None
    assert _pending_from_setting_value({"value": {"bad": True}}) is None
    assert _pending_from_setting_value({"value": "bad"}) is None


def test_confirmation_expiry() -> None:
    pending = PendingConfirmation(
        code="abc123",
        action="increase_amount",
        value="2",
        description="Increase amount",
        created_at=datetime(2026, 5, 6, tzinfo=UTC),
    )

    assert not _is_confirmation_expired(
        pending,
        now=pending.created_at + timedelta(seconds=CONFIRMATION_TTL_SECONDS),
    )
    assert _is_confirmation_expired(
        pending,
        now=pending.created_at + timedelta(seconds=CONFIRMATION_TTL_SECONDS + 1),
    )


def test_pop_pending_confirmation() -> None:
    pending = PendingConfirmation(
        code="abc123",
        action="enable_paper",
        value="true",
        description="Enable paper",
        created_at=datetime(2026, 5, 6, tzinfo=UTC),
    )
    confirmations = {100: pending}

    assert _pop_pending_confirmation(confirmations, 100, "bad", now=pending.created_at) is None
    assert confirmations == {100: pending}
    assert _pop_pending_confirmation(confirmations, 100, "abc123", now=pending.created_at) == pending
    assert confirmations == {}


def test_pop_pending_confirmation_removes_expired() -> None:
    pending = PendingConfirmation(
        code="abc123",
        action="enable_paper",
        value="true",
        description="Enable paper",
        created_at=datetime(2026, 5, 6, tzinfo=UTC),
    )
    confirmations = {100: pending}

    assert (
        _pop_pending_confirmation(
            confirmations,
            100,
            "abc123",
            now=pending.created_at + timedelta(seconds=CONFIRMATION_TTL_SECONDS + 1),
        )
        is None
    )
    assert confirmations == {}
