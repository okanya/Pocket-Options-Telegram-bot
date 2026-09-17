from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class CandleData:
    asset: str
    timeframe_seconds: int
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
    source: str | None = None
    raw_payload: dict[str, Any] | None = None


@dataclass
class CandleBuffer:
    maxlen: int = 500
    _candles: dict[tuple[str, int], deque[CandleData]] = field(default_factory=lambda: defaultdict(deque))

    def add(self, candle: CandleData) -> None:
        key = (candle.asset, candle.timeframe_seconds)
        if self._candles[key].maxlen != self.maxlen:
            self._candles[key] = deque(self._candles[key], maxlen=self.maxlen)

        existing_index = next(
            (index for index, item in enumerate(self._candles[key]) if item.timestamp == candle.timestamp),
            None,
        )
        if existing_index is not None:
            self._candles[key][existing_index] = candle
        else:
            self._candles[key].append(candle)
        self._candles[key] = deque(sorted(self._candles[key], key=lambda item: item.timestamp), maxlen=self.maxlen)

    def extend(self, candles: Iterable[CandleData]) -> None:
        for candle in candles:
            self.add(candle)

    def latest(self, asset: str, timeframe_seconds: int, limit: int | None = None) -> list[CandleData]:
        candles = list(self._candles[(asset, timeframe_seconds)])
        return candles[-limit:] if limit is not None else candles

    def clear(self, asset: str | None = None, timeframe_seconds: int | None = None) -> None:
        if asset is None and timeframe_seconds is None:
            self._candles.clear()
            return
        for key in list(self._candles):
            key_asset, key_timeframe = key
            if (asset is None or key_asset == asset) and (timeframe_seconds is None or key_timeframe == timeframe_seconds):
                del self._candles[key]


def parse_closed_candle(
    payload: dict[str, Any],
    asset: str,
    timeframe_seconds: int,
    source: str | None = "pocket_option",
) -> CandleData | None:
    if _is_open_candle(payload):
        return None

    timestamp = _parse_timestamp(_first_present(payload, "timestamp", "time", "time_open", "from"))
    open_price = _parse_decimal(_first_present(payload, "open", "o"))
    high = _parse_decimal(_first_present(payload, "high", "h"))
    low = _parse_decimal(_first_present(payload, "low", "l"))
    close = _parse_decimal(_first_present(payload, "close", "c", "price"))

    if timestamp is None or open_price is None or high is None or low is None or close is None:
        return None

    return CandleData(
        asset=asset,
        timeframe_seconds=timeframe_seconds,
        timestamp=timestamp,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=_parse_decimal(_first_present(payload, "volume", "v")),
        source=source,
        raw_payload=payload,
    )


def _is_open_candle(payload: dict[str, Any]) -> bool:
    closed = _first_present(payload, "closed", "is_closed", "complete", "is_complete")
    return closed is False


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        if value > 10_000_000_000:
            value = value / 1000
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.isdigit():
            return _parse_timestamp(int(stripped))
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
