from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv


DEFAULT_URL = "wss://demo-api-eu.po.market/socket.io/?EIO=4&transport=websocket"
DEFAULT_MESSAGE_LIMIT = 50
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_PLATFORM = 3


@dataclass(frozen=True)
class ProbeConfig:
    url: str
    ssid: str
    proxy: str | None
    timeout_seconds: float
    message_limit: int
    uid: int
    is_demo: int
    platform: int
    assets: tuple[str, ...]
    period: int


async def main() -> None:
    load_dotenv()
    config = _parse_args()
    started_at = time.monotonic()
    _log(started_at, f"url={config.url}")
    _log(started_at, f"proxy={config.proxy or 'none'}")
    _log(started_at, f"ssid={_mask(config.ssid)}")

    timeout = aiohttp.ClientTimeout(total=config.timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        try:
            async with session.ws_connect(
                config.url,
                proxy=config.proxy,
                headers={
                    "Origin": "https://pocketoption.com",
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    ),
                },
                heartbeat=None,
                receive_timeout=config.timeout_seconds,
            ) as ws:
                _log(started_at, "websocket connected")
                await _probe_socket(ws, config, started_at)
        except Exception as exc:
            _log(started_at, f"ERROR {type(exc).__name__}: {exc}")


async def _probe_socket(ws: aiohttp.ClientWebSocketResponse, config: ProbeConfig, started_at: float) -> None:
    auth_sent = False
    connect_sent = False
    subscriptions_sent = False
    pending_binary_event: str | None = None
    for index in range(1, config.message_limit + 1):
        msg = await ws.receive(timeout=config.timeout_seconds)
        _log(started_at, f"< {index}: {_format_ws_message(msg, pending_binary_event)}")

        if msg.type == aiohttp.WSMsgType.TEXT:
            data = msg.data
            if _has_binary_placeholder(data):
                pending_binary_event = _socketio_event_name(data)
            if data.startswith("0") and not connect_sent:
                await _send(ws, "40", started_at)
                connect_sent = True
            elif data.startswith("40") and not auth_sent:
                await _send(ws, _auth_packet(config), started_at)
                auth_sent = True
            elif data == "2":
                await _send(ws, "3", started_at)
            elif _looks_like_auth_success(data) and not auth_sent:
                await _send(ws, _auth_packet(config), started_at)
                auth_sent = True
        elif msg.type == aiohttp.WSMsgType.BINARY:
            if pending_binary_event == "successauth" and config.assets and not subscriptions_sent:
                await _send_subscriptions(ws, config, started_at)
                subscriptions_sent = True
            pending_binary_event = None

        if msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE}:
            break


async def _send(ws: aiohttp.ClientWebSocketResponse, payload: str, started_at: float) -> None:
    _log(started_at, f"> {_mask_packet(payload)}")
    await ws.send_str(payload)


async def _send_subscriptions(
    ws: aiohttp.ClientWebSocketResponse,
    config: ProbeConfig,
    started_at: float,
) -> None:
    for asset in config.assets:
        change_symbol = "42" + json.dumps(
            ["changeSymbol", {"asset": asset, "period": config.period}],
            separators=(",", ":"),
        )
        subscribe = "42" + json.dumps(["subfor", asset], separators=(",", ":"))
        await _send(ws, change_symbol, started_at)
        await _send(ws, subscribe, started_at)


def _auth_packet(config: ProbeConfig) -> str:
    if config.ssid.startswith("42["):
        return config.ssid
    payload: dict[str, Any] = {
        "session": config.ssid,
        "isDemo": config.is_demo,
        "uid": config.uid,
        "platform": config.platform,
        "isFastHistory": True,
        "isOptimized": True,
    }
    return "42" + json.dumps(["auth", payload], separators=(",", ":"))


def _looks_like_auth_success(data: str) -> bool:
    return data.startswith('42["successauth"') or data.startswith('42["successAuth"')


def _format_ws_message(msg: aiohttp.WSMessage, pending_binary_event: str | None = None) -> str:
    if msg.type == aiohttp.WSMsgType.TEXT:
        return _mask_packet(msg.data)
    if msg.type == aiohttp.WSMsgType.BINARY:
        return _format_binary_payload(pending_binary_event, msg.data)
    return f"{msg.type.name} {msg.data!r}"


def _format_binary_payload(event: str | None, data: bytes) -> str:
    event_label = event or "unknown"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return f"binary event={event_label} len={len(data)} non_utf8=true"

    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        sample = text[:200].replace("\n", "\\n")
        return f"binary event={event_label} len={len(data)} json=false text={sample!r}"

    return f"binary event={event_label} len={len(data)} {_json_summary(event, value)}"


def _json_summary(event: str | None, value: Any) -> str:
    if event == "successupdateBalance" and isinstance(value, dict):
        return f"balance={value.get('balance')} keys={list(value)[:8]}"

    if event == "updateStream":
        return f"stream={_stream_summary(value)}"

    if event == "updateAssets" and isinstance(value, list):
        active_count = sum(1 for row in value if _asset_is_active(row))
        samples = [_asset_summary(row) for row in value[:5] if isinstance(row, list)]
        return (
            f"assets_total={len(value)} active={active_count} "
            f"samples={json.dumps(samples, ensure_ascii=False, separators=(',', ':'))}"
        )

    if event == "successauth":
        return f"json={_short_json(value, max_chars=500)}"

    if isinstance(value, dict):
        return f"json_object keys={list(value)[:10]} sample={_short_json(value)}"

    if isinstance(value, list):
        return f"json_array len={len(value)} sample={_short_json(value[:3])}"

    return f"json_{type(value).__name__} value={_short_json(value)}"


def _stream_summary(value: Any) -> str:
    if not isinstance(value, list):
        return _short_json(value)
    points = []
    for row in value[:5]:
        if isinstance(row, list) and len(row) >= 3:
            points.append({"symbol": row[0], "timestamp": row[1], "price": row[2]})
    return json.dumps(points, ensure_ascii=False, separators=(",", ":"))


def _asset_is_active(row: Any) -> bool:
    return isinstance(row, list) and len(row) > 14 and row[14] is True


def _asset_summary(row: list[Any]) -> dict[str, Any]:
    return {
        "id": _list_get(row, 0),
        "symbol": _list_get(row, 1),
        "name": _list_get(row, 2),
        "type": _list_get(row, 3),
        "payout": _list_get(row, 5),
        "is_otc": _list_get(row, 9) == 1,
        "active": _list_get(row, 14),
        "candles": _list_get(row, 15),
    }


def _list_get(row: list[Any], index: int) -> Any:
    return row[index] if len(row) > index else None


def _short_json(value: Any, max_chars: int = 300) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return encoded if len(encoded) <= max_chars else encoded[:max_chars] + "..."


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


def _mask_packet(packet: str) -> str:
    if "session" not in packet:
        return packet
    try:
        if packet.startswith("42["):
            payload = json.loads(packet[2:])
            if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], dict):
                payload[1]["session"] = _mask(str(payload[1].get("session", "")))
                return "42" + json.dumps(payload, separators=(",", ":"))
    except json.JSONDecodeError:
        pass
    return packet[:80] + "..." if len(packet) > 80 else packet


def _mask(secret: str) -> str:
    if len(secret) <= 10:
        return "***"
    return f"{secret[:5]}...{secret[-5:]} len={len(secret)}"


def _parse_args() -> ProbeConfig:
    parser = argparse.ArgumentParser(description="Raw PocketOption WebSocket handshake probe")
    parser.add_argument("--url", default=os.getenv("POCKET_WS_URL", DEFAULT_URL))
    parser.add_argument("--ssid", default=os.getenv("POCKET_PROBE_SSID") or os.getenv("POCKET_OPTION_SSID"))
    parser.add_argument("--proxy", default=_default_proxy())
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--messages", type=int, default=DEFAULT_MESSAGE_LIMIT)
    parser.add_argument("--uid", type=int, default=int(os.getenv("POCKET_PROBE_UID", "131021432")))
    parser.add_argument("--is-demo", type=int, default=int(os.getenv("POCKET_PROBE_IS_DEMO", "1")))
    parser.add_argument("--platform", type=int, default=int(os.getenv("POCKET_PROBE_PLATFORM", str(DEFAULT_PLATFORM))))
    parser.add_argument(
        "--assets",
        default=os.getenv("POCKET_PROBE_ASSETS", ""),
        help="Comma-separated assets to subscribe after auth, e.g. EURUSD_otc,USDTHB_otc.",
    )
    parser.add_argument("--period", type=int, default=int(os.getenv("POCKET_PROBE_PERIOD", "60")))
    args = parser.parse_args()
    if not args.ssid:
        raise SystemExit("SSID is required. Pass --ssid or set POCKET_PROBE_SSID.")
    assets = tuple(asset.strip() for asset in args.assets.split(",") if asset.strip())
    return ProbeConfig(
        url=args.url,
        ssid=args.ssid,
        proxy=args.proxy or None,
        timeout_seconds=args.timeout,
        message_limit=args.messages,
        uid=args.uid,
        is_demo=args.is_demo,
        platform=args.platform,
        assets=assets,
        period=args.period,
    )


def _default_proxy() -> str | None:
    return (
        os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("ALL_PROXY")
        or os.getenv("all_proxy")
        or None
    )


def _log(started_at: float, message: str) -> None:
    print(f"[{time.monotonic() - started_at:7.2f}s] {message}", flush=True)


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parents[1])
    asyncio.run(main())
