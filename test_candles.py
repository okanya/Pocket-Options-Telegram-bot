from datetime import UTC, datetime
from decimal import Decimal

from app.trading.candles import CandleBuffer, CandleData, parse_closed_candle


def test_parse_closed_candle_from_pocket_payload() -> None:
    candle = parse_closed_candle(
        {
            "time": 1_700_000_000,
            "open": "1.1",
            "high": "1.2",
            "low": "1.0",
            "close": "1.15",
            "volume": "10",
        },
        asset="EURUSD_otc",
        timeframe_seconds=60,
    )

    assert candle == CandleData(
        asset="EURUSD_otc",
        timeframe_seconds=60,
        timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        open=Decimal("1.1"),
        high=Decimal("1.2"),
        low=Decimal("1.0"),
        close=Decimal("1.15"),
        volume=Decimal("10"),
        source="pocket_option",
        raw_payload={
            "time": 1_700_000_000,
            "open": "1.1",
            "high": "1.2",
            "low": "1.0",
            "close": "1.15",
            "volume": "10",
        },
    )


def test_parse_closed_candle_ignores_open_or_invalid_payloads() -> None:
    assert parse_closed_candle({"closed": False}, "EURUSD_otc", 60) is None
    assert parse_closed_candle({"time": 1_700_000_000, "open": "bad"}, "EURUSD_otc", 60) is None


def test_candle_buffer_sorts_limits_and_replaces_by_timestamp() -> None:
    buffer = CandleBuffer(maxlen=2)
    first = CandleData(
        asset="EURUSD_otc",
        timeframe_seconds=60,
        timestamp=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        open=Decimal("1"),
        high=Decimal("2"),
        low=Decimal("0.5"),
        close=Decimal("1.5"),
    )
    second = CandleData(
        asset="EURUSD_otc",
        timeframe_seconds=60,
        timestamp=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        open=Decimal("2"),
        high=Decimal("3"),
        low=Decimal("1.5"),
        close=Decimal("2.5"),
    )
    replacement = CandleData(
        asset="EURUSD_otc",
        timeframe_seconds=60,
        timestamp=first.timestamp,
        open=Decimal("9"),
        high=Decimal("9"),
        low=Decimal("9"),
        close=Decimal("9"),
    )
    third = CandleData(
        asset="EURUSD_otc",
        timeframe_seconds=60,
        timestamp=datetime(2026, 1, 1, 0, 2, tzinfo=UTC),
        open=Decimal("3"),
        high=Decimal("4"),
        low=Decimal("2.5"),
        close=Decimal("3.5"),
    )

    buffer.extend([second, first, replacement, third])

    assert buffer.latest("EURUSD_otc", 60) == [second, third]
