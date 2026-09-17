from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import aiohttp

from app.trading.pocket_client import PocketClientError, PocketClientNotConnectedError

logger = logging.getLogger(__name__)

DEFAULT_POCKET_WS_URL = "wss://demo-api-eu.po.market/socket.io/?EIO=4&transport=websocket"
DEFAULT_PLATFORM = 3
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_HEARTBEAT_SECONDS = 20.0


@dataclass
class _LiveCandle:
    timestamp: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    @classmethod
    def from_tick(cls, timestamp: int, price: Decimal, timeframe_seconds: int) -> _LiveCandle:
        bucket = timestamp - timestamp % timeframe_seconds
        return cls(timestamp=bucket, open=price, high=price, low=price, close=price)

    def update(self, price: Decimal) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price

    def to_payload(self, asset: str, timeframe_seconds: int, raw_payload: Any | None = None) -> dict[str, Any]:
        return {
            "asset": asset,
            "timeframe_seconds": timeframe_seconds,
            "timestamp": self.timestamp,
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "closed": True,
            "source": "pocket_option_raw",
            "raw_payload": raw_payload,
        }


@dataclass(frozen=True)
class _SubscriptionClosed:
    error: Exception | None = None


class RawPocketClient:
    """Minimal read-only PocketOption Socket.IO client implemented in Python."""

    def __init__(
        self,
        ssid: str,
        *,
        url: str | None = None,
        proxy: str | None = None,
        timeout_seconds: float | None = None,
        heartbeat_seconds: float | None = None,
        uid: int = 131021432,
        is_demo: int = 1,
        platform: int = DEFAULT_PLATFORM,
        connect_attempts: int = 3,
        session_factory: Callable[..., aiohttp.ClientSession] = aiohttp.ClientSession,
    ) -> None:
        self.ssid = ssid
        self.url = url or _default_ws_url()
        self.proxy = proxy if proxy is not None else _default_proxy()
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else _default_timeout_seconds()
        self.heartbeat_seconds = heartbeat_seconds if heartbeat_seconds is not None else _default_heartbeat_seconds()
        self.uid = uid
        self.is_demo = is_demo
        self.platform = platform
        self.connect_attempts = max(1, connect_attempts)
        self._session_factory = session_factory
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._connected = False
        self._auth_event = asyncio.Event()
        self._balance_event = asyncio.Event()
        self._balance: Decimal | None = None
        self._assets: dict[str, dict[str, Any]] = {}
        self._subscriptions: dict[str, asyncio.Queue[dict[str, Any] | _SubscriptionClosed]] = {}
        self._timeframes: dict[str, int] = {}
        self._live_candles: dict[str, _LiveCandle] = {}
        self._pending_orders: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._failure_matching: dict[tuple[str, str], deque[str]] = {}
        self._order_meta: dict[str, tuple[str, str]] = {}
        self._closed_deals: dict[str, dict[str, Any]] = {}
        self._closed_deal_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pending_binary_event: str | None = None
        self._closed_event = asyncio.Event()
        self._reader_error: Exception | None = None

    async def connect(self) -> None:
        if self._connected:
            return
        self._reset_runtime_state()
        await self._connect_websocket()
        self._reader_task = asyncio.create_task(self._read_loop(), name="pocket-raw-reader")
        try:
            await self._wait_for_auth()
        except TimeoutError as exc:
            await self.disconnect()
            raise PocketClientError("PocketOption raw auth timed out") from exc
        except Exception:
            await self.disconnect()
            raise
        self._connected = True
        logger.info("PocketOption raw client connected")

    async def disconnect(self) -> None:
        self._connected = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_balance(self) -> Decimal | None:
        self._require_connected()
        if self._balance is None:
            try:
                await asyncio.wait_for(self._balance_event.wait(), timeout=5.0)
            except TimeoutError:
                return None
        return self._balance

    async def get_payout(self, asset: str) -> Decimal | None:
        self._require_connected()
        payout = self._assets.get(asset, {}).get("payout")
        return Decimal(str(payout)) / Decimal("100") if payout is not None else None

    async def get_payouts(self) -> dict[str, Decimal | None]:
        self._require_connected()
        result: dict[str, Decimal | None] = {}
        for asset, payload in self._assets.items():
            payout = payload.get("payout")
            result[asset] = Decimal(str(payout)) / Decimal("100") if payout is not None else None
        return result

    async def subscribe_candles(self, asset: str, timeframe_seconds: int) -> AsyncIterator[dict[str, Any]]:
        self._require_connected()
        queue = self._subscriptions.setdefault(asset, asyncio.Queue(maxsize=256))
        self._timeframes[asset] = timeframe_seconds
        await self._send_change_symbol(asset, timeframe_seconds)
        while True:
            item = await queue.get()
            if isinstance(item, _SubscriptionClosed):
                if item.error is not None:
                    raise PocketClientError(f"PocketOption raw subscription closed: {item.error}") from item.error
                return
            yield item

    async def buy(self, asset: str, amount: Decimal, expiration_seconds: int) -> tuple[str, dict[str, Any]]:
        return await self._open_order(asset, amount, expiration_seconds, action="call")

    async def sell(self, asset: str, amount: Decimal, expiration_seconds: int) -> tuple[str, dict[str, Any]]:
        return await self._open_order(asset, amount, expiration_seconds, action="put")

    async def check_win(self, trade_id: str, expiration_seconds: int | None = None) -> dict[str, Any] | None:
        self._require_connected()
        if trade_id in self._closed_deals:
            return _deal_result_payload(self._closed_deals[trade_id])
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._closed_deal_waiters[trade_id] = waiter
        timeout_seconds = max(60, (expiration_seconds or 60) + 30)
        try:
            deal = await asyncio.wait_for(waiter, timeout=timeout_seconds)
        finally:
            self._closed_deal_waiters.pop(trade_id, None)
        return _deal_result_payload(deal)

    def is_connected(self) -> bool:
        return self._connected

    async def _read_loop(self) -> None:
        ws = self._require_ws()
        try:
            while True:
                msg = await ws.receive(timeout=self.timeout_seconds)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_text(str(msg.data))
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await self._handle_binary(bytes(msg.data), self._pending_binary_event)
                    self._pending_binary_event = None
                elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE}:
                    raise PocketClientError(_websocket_close_message(ws, msg))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._mark_connection_failed(exc)

    async def _connect_websocket(self) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self.connect_attempts + 1):
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = self._session_factory(timeout=timeout, trust_env=False)
            try:
                self._ws = await self._session.ws_connect(
                    self.url,
                    proxy=self.proxy,
                    headers={
                        "Origin": "https://pocketoption.com",
                        "User-Agent": (
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                        ),
                    },
                    heartbeat=self.heartbeat_seconds,
                    receive_timeout=self.timeout_seconds,
                )
                return
            except Exception as exc:
                last_error = exc
                await self.disconnect()
                if attempt < self.connect_attempts:
                    await asyncio.sleep(min(2.0, 0.5 * attempt))
        if last_error is not None:
            raise last_error

    async def _handle_text(self, data: str) -> None:
        if _has_binary_placeholder(data):
            self._pending_binary_event = _socketio_event_name(data)
        if data.startswith("0"):
            await self._send("40")
        elif data.startswith("40"):
            await self._send(_auth_packet(self.ssid, uid=self.uid, is_demo=self.is_demo, platform=self.platform))
        elif data == "2":
            await self._send("3")
        elif data.startswith("42"):
            payload = _socketio_payload(data)
            if isinstance(payload, list) and payload:
                await self._handle_event(str(payload[0]), payload[1] if len(payload) > 1 else None)

    async def _handle_binary(self, data: bytes, event: str | None) -> None:
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.debug("Skipping non-JSON PocketOption binary payload for event %s", event)
            return
        await self._handle_event(event or "", payload)

    async def _handle_event(self, event: str, payload: Any) -> None:
        if event == "successauth":
            self._auth_event.set()
        elif event == "successupdateBalance" and isinstance(payload, dict):
            balance = payload.get("balance")
            self._balance = Decimal(str(balance)) if balance is not None else None
            self._balance_event.set()
        elif event == "updateAssets" and isinstance(payload, list):
            self._assets = _parse_assets(payload)
        elif event in {"updateHistoryNewFast", "updateHistoryNew", "updateHistory"} and isinstance(payload, dict):
            await self._handle_history(payload)
        elif event == "updateStream":
            await self._handle_stream(payload)
        elif event == "successopenOrder" and isinstance(payload, dict):
            self._handle_open_order_success(payload)
        elif event == "failopenOrder" and isinstance(payload, dict):
            self._handle_open_order_failure(payload)
        elif event == "updateClosedDeals" and isinstance(payload, list):
            self._handle_closed_deals(payload)
        elif event == "successcloseOrder":
            self._handle_closed_deals_payload(payload)

    async def _handle_history(self, payload: dict[str, Any]) -> None:
        asset = str(payload.get("asset") or "")
        queue = self._subscriptions.get(asset)
        if queue is None:
            return
        timeframe_seconds = int(payload.get("period") or self._timeframes.get(asset) or 60)
        for candle in _history_payload_to_candles(payload, asset=asset, timeframe_seconds=timeframe_seconds):
            await _put_latest(queue, candle)

    async def _handle_stream(self, payload: Any) -> None:
        if not isinstance(payload, list):
            return
        for item in payload:
            if not isinstance(item, list) or len(item) < 3:
                continue
            asset = str(item[0])
            queue = self._subscriptions.get(asset)
            timeframe_seconds = self._timeframes.get(asset)
            if queue is None or timeframe_seconds is None:
                continue
            tick_timestamp = int(float(item[1]))
            price = Decimal(str(item[2]))
            candle = _LiveCandle.from_tick(tick_timestamp, price, timeframe_seconds)
            current = self._live_candles.get(asset)
            if current is None:
                self._live_candles[asset] = candle
            elif candle.timestamp == current.timestamp:
                current.update(price)
            else:
                await _put_latest(queue, current.to_payload(asset, timeframe_seconds, raw_payload=item))
                self._live_candles[asset] = candle

    async def _send_change_symbol(self, asset: str, timeframe_seconds: int) -> None:
        await self._send(
            "42"
            + json.dumps(
                ["changeSymbol", {"asset": asset, "period": timeframe_seconds}],
                separators=(",", ":"),
            )
        )
        await self._send("42" + json.dumps(["subfor", asset], separators=(",", ":")))

    async def _open_order(
        self,
        asset: str,
        amount: Decimal,
        expiration_seconds: int,
        *,
        action: str,
    ) -> tuple[str, dict[str, Any]]:
        self._require_connected()
        request_id = str(uuid.uuid4())
        normalized_amount = _normalize_decimal_key(amount)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_orders[request_id] = future
        self._order_meta[request_id] = (asset, normalized_amount)
        self._failure_matching.setdefault((asset, normalized_amount), deque()).append(request_id)
        try:
            await self._send(
                _open_order_packet(
                    asset=asset,
                    amount=amount,
                    action=action,
                    is_demo=self.is_demo,
                    request_id=request_id,
                    expiration_seconds=expiration_seconds,
                )
            )
            response = await asyncio.wait_for(future, timeout=30.0)
        except Exception:
            self._clear_pending_order(request_id)
            raise
        trade_id = str(response.get("id") or "")
        if not trade_id:
            raise PocketClientError("PocketOption open order response does not contain trade id")
        return trade_id, response

    def _handle_open_order_success(self, payload: dict[str, Any]) -> None:
        request_id = str(payload.get("requestId") or payload.get("request_id") or "")
        if not request_id:
            return
        future = self._pending_orders.get(request_id)
        if future is None or future.done():
            return
        self._clear_pending_order(request_id)
        future.set_result(payload)

    def _handle_open_order_failure(self, payload: dict[str, Any]) -> None:
        asset = str(payload.get("asset") or "")
        amount = _normalize_decimal_key(payload.get("amount"))
        request_id = _pop_left_matching_request(self._failure_matching, asset, amount)
        if request_id is None:
            return
        future = self._pending_orders.get(request_id)
        if future is None or future.done():
            return
        self._clear_pending_order(request_id)
        future.set_exception(PocketClientError(str(payload.get("error") or "PocketOption open order failed")))

    def _handle_closed_deals_payload(self, payload: Any) -> None:
        if isinstance(payload, dict) and isinstance(payload.get("deals"), list):
            self._handle_closed_deals(payload["deals"])
        elif isinstance(payload, list):
            self._handle_closed_deals(payload)

    def _handle_closed_deals(self, payload: list[Any]) -> None:
        for deal in payload:
            if not isinstance(deal, dict):
                continue
            trade_id = str(deal.get("id") or "")
            if not trade_id:
                continue
            self._closed_deals[trade_id] = deal
            waiter = self._closed_deal_waiters.get(trade_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(deal)

    def _clear_pending_order(self, request_id: str) -> None:
        self._pending_orders.pop(request_id, None)
        meta = self._order_meta.pop(request_id, None)
        if meta is None:
            return
        queue = self._failure_matching.get(meta)
        if queue is None:
            return
        try:
            queue.remove(request_id)
        except ValueError:
            pass
        if not queue:
            self._failure_matching.pop(meta, None)

    async def _send(self, payload: str) -> None:
        ws = self._require_ws()
        await ws.send_str(payload)

    async def _wait_for_auth(self) -> None:
        if self._reader_task is None:
            raise PocketClientNotConnectedError("PocketClient is not connected")
        auth_task = asyncio.create_task(self._auth_event.wait())
        done, pending = await asyncio.wait(
            {auth_task, self._reader_task},
            timeout=self.timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            if task is auth_task:
                task.cancel()
        if not done:
            auth_task.cancel()
            raise TimeoutError
        if self._reader_task in done:
            await self._reader_task
            if self._reader_error is not None:
                raise PocketClientError("PocketOption raw connection failed before auth") from self._reader_error
            raise PocketClientError("PocketOption raw connection closed before auth")

    async def _mark_connection_failed(self, exc: Exception) -> None:
        if self._closed_event.is_set():
            return
        self._reader_error = exc
        self._connected = False
        self._closed_event.set()
        logger.warning("PocketOption raw connection closed: %s", exc)
        for queue in self._subscriptions.values():
            await _put_latest(queue, _SubscriptionClosed(error=exc))
        for future in self._pending_orders.values():
            if not future.done():
                order_error = PocketClientError("PocketOption raw connection closed during order")
                order_error.__cause__ = exc
                future.set_exception(order_error)
        for future in self._closed_deal_waiters.values():
            if not future.done():
                result_error = PocketClientError("PocketOption raw connection closed while waiting for trade result")
                result_error.__cause__ = exc
                future.set_exception(result_error)
        self._pending_orders.clear()
        self._failure_matching.clear()
        self._order_meta.clear()
        self._closed_deal_waiters.clear()

    def _reset_runtime_state(self) -> None:
        self._auth_event = asyncio.Event()
        self._balance_event = asyncio.Event()
        self._closed_event = asyncio.Event()
        self._reader_error = None
        self._pending_binary_event = None

    def _require_connected(self) -> None:
        if not self._connected:
            raise PocketClientNotConnectedError("PocketClient is not connected")

    def _require_ws(self) -> aiohttp.ClientWebSocketResponse:
        if self._ws is None:
            raise PocketClientNotConnectedError("PocketClient is not connected")
        return self._ws


def _auth_packet(ssid: str, *, uid: int, is_demo: int, platform: int) -> str:
    if ssid.startswith("42["):
        return ssid
    return "42" + json.dumps(
        [
            "auth",
            {
                "session": ssid,
                "isDemo": is_demo,
                "uid": uid,
                "platform": platform,
                "isFastHistory": True,
                "isOptimized": True,
            },
        ],
        separators=(",", ":"),
    )


def _open_order_packet(
    *,
    asset: str,
    amount: Decimal,
    action: str,
    is_demo: int,
    request_id: str,
    expiration_seconds: int,
) -> str:
    return "42" + json.dumps(
        [
            "openOrder",
            {
                "asset": asset,
                "action": action,
                "amount": float(amount),
                "isDemo": is_demo,
                "optionType": 100,
                "requestId": request_id,
                "time": expiration_seconds,
            },
        ],
        separators=(",", ":"),
    )


def _deal_result_payload(deal: dict[str, Any]) -> dict[str, Any]:
    profit = _decimal_or_none(deal.get("profit"))
    return {
        **deal,
        "result": _result_from_profit(profit),
        "profit": str(profit) if profit is not None else None,
        "open_price": deal.get("openPrice") or deal.get("open_price"),
        "close_price": deal.get("closePrice") or deal.get("close_price"),
    }


def _result_from_profit(profit: Decimal | None) -> str:
    if profit is None:
        return "unknown"
    if profit > 0:
        return "win"
    if profit < 0:
        return "loss"
    return "draw"


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _normalize_decimal_key(value: Any) -> str:
    decimal_value = _decimal_or_none(value)
    return str(decimal_value.normalize()) if decimal_value is not None else str(value)


def _pop_left_matching_request(
    failure_matching: dict[tuple[str, str], deque[str]],
    asset: str,
    amount: str,
) -> str | None:
    queue = failure_matching.get((asset, amount))
    if queue is None:
        return None
    request_id = queue.popleft() if queue else None
    if not queue:
        failure_matching.pop((asset, amount), None)
    return request_id


def _parse_assets(rows: list[Any]) -> dict[str, dict[str, Any]]:
    assets: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 16:
            continue
        symbol = str(row[1])
        assets[symbol] = {
            "id": row[0],
            "symbol": symbol,
            "name": row[2],
            "type": row[3],
            "payout": row[5],
            "is_otc": row[9] == 1,
            "active": row[14] is True,
            "candles": row[15],
        }
    return assets


def _history_payload_to_candles(
    payload: dict[str, Any],
    *,
    asset: str,
    timeframe_seconds: int,
) -> list[dict[str, Any]]:
    raw_candles = payload.get("candles")
    if isinstance(raw_candles, list) and raw_candles:
        return [
            _base_candle_to_payload(candle, asset=asset, timeframe_seconds=timeframe_seconds, raw_payload=payload)
            for candle in raw_candles[:-1]
            if isinstance(candle, list)
        ]
    history = payload.get("history")
    if isinstance(history, list):
        return _ticks_to_closed_candles(history, asset=asset, timeframe_seconds=timeframe_seconds, raw_payload=payload)
    return []


def _base_candle_to_payload(
    row: list[Any],
    *,
    asset: str,
    timeframe_seconds: int,
    raw_payload: Any,
) -> dict[str, Any]:
    timestamp, open_price, close_price, high, low, *rest = row
    return {
        "asset": asset,
        "timeframe_seconds": timeframe_seconds,
        "timestamp": int(float(timestamp)),
        "open": str(open_price),
        "high": str(high),
        "low": str(low),
        "close": str(close_price),
        "volume": str(rest[0]) if rest else None,
        "closed": True,
        "source": "pocket_option_raw",
        "raw_payload": raw_payload,
    }


def _ticks_to_closed_candles(
    ticks: list[Any],
    *,
    asset: str,
    timeframe_seconds: int,
    raw_payload: Any,
) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    current: _LiveCandle | None = None
    for item in sorted((tick for tick in ticks if isinstance(tick, list) and len(tick) >= 2), key=lambda tick: float(tick[0])):
        timestamp = int(float(item[0]))
        price = Decimal(str(item[1]))
        next_candle = _LiveCandle.from_tick(timestamp, price, timeframe_seconds)
        if current is None:
            current = next_candle
        elif current.timestamp == next_candle.timestamp:
            current.update(price)
        else:
            candles.append(current.to_payload(asset, timeframe_seconds, raw_payload=raw_payload))
            current = next_candle
    return candles[:-1] if len(candles) > 1 else []


async def _put_latest(
    queue: asyncio.Queue[dict[str, Any] | _SubscriptionClosed],
    payload: dict[str, Any] | _SubscriptionClosed,
) -> None:
    if queue.full():
        queue.get_nowait()
    await queue.put(payload)


def _socketio_event_name(packet: str) -> str | None:
    payload = _socketio_payload(packet)
    if isinstance(payload, list) and payload and isinstance(payload[0], str):
        return payload[0]
    return None


def _has_binary_placeholder(packet: str) -> bool:
    payload = _socketio_payload(packet)
    if not isinstance(payload, list):
        return False
    return any(isinstance(item, dict) and item.get("_placeholder") is True for item in payload[1:])


def _socketio_payload(packet: str) -> Any | None:
    start = packet.find("[")
    if start < 0:
        return None
    try:
        return json.loads(packet[start:])
    except json.JSONDecodeError:
        return None


def _default_proxy() -> str | None:
    explicit_proxy = os.getenv("POCKET_OPTION_PROXY")
    if explicit_proxy:
        return explicit_proxy
    if not _env_flag("POCKET_OPTION_USE_ENV_PROXY"):
        return None
    return os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or os.getenv("ALL_PROXY") or os.getenv("all_proxy") or None


def _default_ws_url() -> str:
    return os.getenv("POCKET_OPTION_WS_URL") or DEFAULT_POCKET_WS_URL


def _default_timeout_seconds() -> float:
    return _env_float("POCKET_OPTION_WS_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, minimum=5.0)


def _default_heartbeat_seconds() -> float | None:
    value = _env_float("POCKET_OPTION_WS_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS, minimum=0.0)
    return value if value > 0 else None


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default
    if value < minimum:
        logger.warning("Ignoring too small %s=%r; using %s", name, raw, default)
        return default
    return value


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _websocket_close_message(ws: aiohttp.ClientWebSocketResponse, msg: aiohttp.WSMessage) -> str:
    parts = [f"PocketOption raw websocket closed: {msg.type.name}"]
    if ws.close_code is not None:
        parts.append(f"code={ws.close_code}")
    if msg.extra:
        parts.append(f"reason={msg.extra}")
    if ws.exception() is not None:
        parts.append(f"exception={ws.exception()}")
    return " ".join(parts)
