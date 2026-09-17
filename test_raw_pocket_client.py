import asyncio
from decimal import Decimal

import pytest

from app.trading.pocket_client import PocketClientError
from app.trading.raw_pocket_client import (
    RawPocketClient,
    _auth_packet,
    _base_candle_to_payload,
    _default_heartbeat_seconds,
    _default_proxy,
    _default_timeout_seconds,
    _default_ws_url,
    _deal_result_payload,
    _history_payload_to_candles,
    _open_order_packet,
    _parse_assets,
    _ticks_to_closed_candles,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_str(self, payload: str) -> None:
        self.sent.append(payload)


def test_auth_packet_accepts_full_socketio_auth() -> None:
    packet = '42["auth",{"session":"session-secret","isDemo":1}]'

    assert _auth_packet(packet, uid=1, is_demo=1, platform=3) == packet


def test_auth_packet_wraps_raw_session() -> None:
    packet = _auth_packet("session-secret", uid=123, is_demo=1, platform=3)

    assert packet == (
        '42["auth",{"session":"session-secret","isDemo":1,"uid":123,'
        '"platform":3,"isFastHistory":true,"isOptimized":true}]'
    )


def test_open_order_packet_matches_socketio_wire_format() -> None:
    packet = _open_order_packet(
        asset="EURUSD_otc",
        amount=Decimal("10"),
        action="call",
        is_demo=1,
        request_id="request-1",
        expiration_seconds=60,
    )

    assert packet == (
        '42["openOrder",{"asset":"EURUSD_otc","action":"call","amount":10.0,'
        '"isDemo":1,"optionType":100,"requestId":"request-1","time":60}]'
    )


def test_default_proxy_ignores_generic_proxy_env_by_default(monkeypatch) -> None:
    monkeypatch.delenv("POCKET_OPTION_PROXY", raising=False)
    monkeypatch.delenv("POCKET_OPTION_USE_ENV_PROXY", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:12334/")
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:12334/")

    assert _default_proxy() is None


def test_default_proxy_uses_explicit_pocket_option_proxy(monkeypatch) -> None:
    monkeypatch.setenv("POCKET_OPTION_PROXY", "socks://127.0.0.1:12334/")
    monkeypatch.setenv("HTTPS_PROXY", "http://ignored.local:8080/")

    assert _default_proxy() == "socks://127.0.0.1:12334/"


def test_default_proxy_can_opt_in_to_generic_proxy_env(monkeypatch) -> None:
    monkeypatch.delenv("POCKET_OPTION_PROXY", raising=False)
    monkeypatch.setenv("POCKET_OPTION_USE_ENV_PROXY", "true")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:12334/")

    assert _default_proxy() == "http://127.0.0.1:12334/"


def test_default_ws_options_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("POCKET_OPTION_WS_URL", "wss://example.test/socket.io/?EIO=4&transport=websocket")
    monkeypatch.setenv("POCKET_OPTION_WS_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("POCKET_OPTION_WS_HEARTBEAT_SECONDS", "25")

    assert _default_ws_url() == "wss://example.test/socket.io/?EIO=4&transport=websocket"
    assert _default_timeout_seconds() == 90
    assert _default_heartbeat_seconds() == 25


def test_default_ws_heartbeat_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("POCKET_OPTION_WS_HEARTBEAT_SECONDS", "0")

    assert _default_heartbeat_seconds() is None


def test_parse_assets_uses_pocket_option_tuple_indexes() -> None:
    assets = _parse_assets(
        [
            [
                170,
                "#AAPL_otc",
                "Apple OTC",
                "stock",
                None,
                92,
                None,
                None,
                None,
                1,
                None,
                None,
                None,
                None,
                True,
                [{"time": 60}],
            ]
        ]
    )

    assert assets["#AAPL_otc"] == {
        "id": 170,
        "symbol": "#AAPL_otc",
        "name": "Apple OTC",
        "type": "stock",
        "payout": 92,
        "is_otc": True,
        "active": True,
        "candles": [{"time": 60}],
    }


def test_base_candle_array_parses_server_order() -> None:
    payload = _base_candle_to_payload(
        [1_700_000_000, 1.1, 1.15, 1.2, 1.0, 12],
        asset="EURUSD_otc",
        timeframe_seconds=60,
        raw_payload={"source": "test"},
    )

    assert payload == {
        "asset": "EURUSD_otc",
        "timeframe_seconds": 60,
        "timestamp": 1_700_000_000,
        "open": "1.1",
        "high": "1.2",
        "low": "1.0",
        "close": "1.15",
        "volume": "12",
        "closed": True,
        "source": "pocket_option_raw",
        "raw_payload": {"source": "test"},
    }


def test_history_payload_excludes_last_potentially_open_candle() -> None:
    payload = {
        "asset": "EURUSD_otc",
        "period": 60,
        "candles": [
            [1_700_000_000, 1.1, 1.15, 1.2, 1.0],
            [1_700_000_060, 1.15, 1.16, 1.17, 1.14],
        ],
    }

    candles = _history_payload_to_candles(payload, asset="EURUSD_otc", timeframe_seconds=60)

    assert len(candles) == 1
    assert candles[0]["timestamp"] == 1_700_000_000
    assert candles[0]["close"] == "1.15"


def test_ticks_compile_to_closed_candles() -> None:
    ticks = [
        [1_700_000_000.1, 1.1],
        [1_700_000_010.1, 1.2],
        [1_700_000_060.1, 1.15],
        [1_700_000_120.1, 1.3],
    ]

    candles = _ticks_to_closed_candles(
        ticks,
        asset="EURUSD_otc",
        timeframe_seconds=60,
        raw_payload={"source": "test"},
    )

    assert candles[0]["timestamp"] == 1_699_999_980
    assert candles[0]["open"] == str(Decimal("1.1"))
    assert candles[0]["high"] == str(Decimal("1.2"))
    assert candles[0]["low"] == str(Decimal("1.1"))
    assert candles[0]["close"] == str(Decimal("1.2"))


@pytest.mark.asyncio
async def test_subscription_raises_when_reader_fails() -> None:
    client = RawPocketClient("session-secret")
    fake_ws = FakeWebSocket()
    client._ws = fake_ws
    client._connected = True

    subscription = client.subscribe_candles("EURUSD_otc", 60)
    next_item = asyncio.create_task(subscription.__anext__())
    await asyncio.sleep(0)
    await client._mark_connection_failed(PocketClientError("websocket closed"))

    with pytest.raises(PocketClientError, match="subscription closed"):
        await next_item
    assert fake_ws.sent == [
        '42["changeSymbol",{"asset":"EURUSD_otc","period":60}]',
        '42["subfor","EURUSD_otc"]',
    ]


@pytest.mark.asyncio
async def test_connection_failure_wakes_pending_order() -> None:
    client = RawPocketClient("session-secret")
    client._ws = FakeWebSocket()
    client._connected = True

    open_order = asyncio.create_task(client.buy("EURUSD_otc", Decimal("1"), 60))
    await asyncio.sleep(0)
    await client._mark_connection_failed(PocketClientError("websocket closed"))

    with pytest.raises(PocketClientError, match="during order"):
        await open_order


@pytest.mark.asyncio
async def test_connection_failure_wakes_check_win_waiter() -> None:
    client = RawPocketClient("session-secret")
    client._connected = True

    check_result = asyncio.create_task(client.check_win("trade-1", expiration_seconds=1))
    await asyncio.sleep(0)
    await client._mark_connection_failed(PocketClientError("websocket closed"))

    with pytest.raises(PocketClientError, match="waiting for trade result"):
        await check_result


@pytest.mark.asyncio
async def test_buy_waits_for_successopenorder_response() -> None:
    client = RawPocketClient("session-secret")
    fake_ws = FakeWebSocket()
    client._ws = fake_ws
    client._connected = True

    open_order = asyncio.create_task(client.buy("EURUSD_otc", Decimal("1"), 60))
    await asyncio.sleep(0)
    sent_payload = fake_ws.sent[0]
    request_id = sent_payload.split('"requestId":"', 1)[1].split('"', 1)[0]
    await client._handle_event(
        "successopenOrder",
        {
            "id": "trade-1",
            "requestId": request_id,
            "asset": "EURUSD_otc",
            "openPrice": "1.1",
            "profit": "0.92",
        },
    )

    assert await open_order == (
        "trade-1",
        {
            "id": "trade-1",
            "requestId": request_id,
            "asset": "EURUSD_otc",
            "openPrice": "1.1",
            "profit": "0.92",
        },
    )


@pytest.mark.asyncio
async def test_buy_matches_failopenorder_by_asset_and_amount() -> None:
    client = RawPocketClient("session-secret")
    client._ws = FakeWebSocket()
    client._connected = True

    open_order = asyncio.create_task(client.buy("EURUSD_otc", Decimal("1.0"), 60))
    await asyncio.sleep(0)
    await client._handle_event(
        "failopenOrder",
        {
            "asset": "EURUSD_otc",
            "amount": 1,
            "error": "Insufficient balance",
        },
    )

    with pytest.raises(PocketClientError, match="Insufficient balance"):
        await open_order


@pytest.mark.asyncio
async def test_check_win_returns_closed_deal_result() -> None:
    client = RawPocketClient("session-secret")
    client._connected = True
    client._closed_deals["trade-1"] = {
        "id": "trade-1",
        "profit": "9.2",
        "openPrice": "1.1",
        "closePrice": "1.2",
    }

    assert await client.check_win("trade-1", expiration_seconds=60) == {
        "id": "trade-1",
        "profit": "9.2",
        "openPrice": "1.1",
        "closePrice": "1.2",
        "result": "win",
        "open_price": "1.1",
        "close_price": "1.2",
    }


@pytest.mark.asyncio
async def test_successcloseorder_payload_wakes_check_win_waiter() -> None:
    client = RawPocketClient("session-secret")
    client._connected = True

    check_result = asyncio.create_task(client.check_win("trade-1", expiration_seconds=1))
    await asyncio.sleep(0)
    await client._handle_event(
        "successcloseOrder",
        {
            "profit": -1,
            "deals": [
                {
                    "id": "trade-1",
                    "profit": -1,
                    "openPrice": 1.2,
                    "closePrice": 1.1,
                }
            ],
        },
    )

    assert await check_result == {
        "id": "trade-1",
        "profit": "-1",
        "openPrice": 1.2,
        "closePrice": 1.1,
        "result": "loss",
        "open_price": 1.2,
        "close_price": 1.1,
    }


def test_deal_result_payload_maps_profit_to_result() -> None:
    assert _deal_result_payload({"profit": "-1"})["result"] == "loss"
    assert _deal_result_payload({"profit": "0"})["result"] == "draw"
    assert _deal_result_payload({"profit": "1"})["result"] == "win"
