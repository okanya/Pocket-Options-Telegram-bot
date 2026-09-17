from decimal import Decimal

import pytest

from app.trading.pocket_client import (
    PocketClient,
    TradingOperationDisabledError,
    _check_win_timeout,
)


@pytest.mark.asyncio
async def test_mocked_mode_without_ssid() -> None:
    client = PocketClient(ssid=None)

    await client.connect()

    assert client.mocked is True
    assert client.is_connected() is True
    assert await client.get_balance() is None
    assert await client.get_payout("EURUSD_otc") is None


@pytest.mark.asyncio
async def test_buy_and_sell_are_disabled_in_mocked_mode() -> None:
    client = PocketClient(ssid=None)

    with pytest.raises(TradingOperationDisabledError):
        await client.buy("EURUSD_otc", Decimal("1"), 60)

    with pytest.raises(TradingOperationDisabledError):
        await client.sell("EURUSD_otc", Decimal("1"), 60)


@pytest.mark.asyncio
async def test_real_client_import_path_is_wrapped(monkeypatch) -> None:
    created = {}

    class FakeRawPocketClient:
        def __init__(self, ssid):
            created["ssid"] = ssid
            self._connected = False

        async def connect(self):
            created["connected"] = True
            self._connected = True

        async def disconnect(self):
            created["disconnected"] = True
            self._connected = False

        async def get_balance(self):
            return Decimal("123.45")

        async def get_payout(self, asset):
            created["payout_asset"] = asset
            return Decimal("0.82")

        async def get_payouts(self):
            created["payout_asset"] = None
            return {"EURUSD_otc": Decimal("0.82"), "GBPUSD_otc": None}

        async def buy(self, asset, amount, expiration_seconds):
            created["buy"] = (asset, amount, expiration_seconds)
            return "trade-buy", {"id": "trade-buy", "openPrice": "1.1"}

        async def sell(self, asset, amount, expiration_seconds):
            created["sell"] = (asset, amount, expiration_seconds)
            return "trade-sell", {"id": "trade-sell", "openPrice": "1.2"}

        async def check_win(self, trade_id, expiration_seconds=None):
            created["check_win"] = (trade_id, expiration_seconds)
            return {"result": "win", "profit": "8", "closePrice": "1.3"}

        def is_connected(self):
            return self._connected

    monkeypatch.setattr("app.trading.raw_pocket_client.RawPocketClient", FakeRawPocketClient)

    client = PocketClient(ssid="ssid-secret")

    await client.connect()

    assert created == {"ssid": "ssid-secret", "connected": True}
    assert client.mocked is False
    assert client.is_connected() is True
    assert await client.get_balance() == Decimal("123.45")
    assert await client.get_payout("EURUSD_otc") == Decimal("0.82")
    assert created["payout_asset"] == "EURUSD_otc"
    assert await client.get_payouts() == {"EURUSD_otc": Decimal("0.82"), "GBPUSD_otc": None}
    assert created["payout_asset"] is None
    assert await client.buy("EURUSD_otc", Decimal("1"), 60) == ("trade-buy", {"id": "trade-buy", "openPrice": "1.1"})
    assert created["buy"] == ("EURUSD_otc", Decimal("1"), 60)
    assert await client.sell("EURUSD_otc", Decimal("2"), 120) == ("trade-sell", {"id": "trade-sell", "openPrice": "1.2"})
    assert created["sell"] == ("EURUSD_otc", Decimal("2"), 120)
    assert await client.check_win("trade-buy", expiration_seconds=60) == {
        "result": "win",
        "profit": "8",
        "closePrice": "1.3",
    }
    assert created["check_win"] == ("trade-buy", 60)


@pytest.mark.asyncio
async def test_trading_operations_require_connected_client() -> None:
    client = PocketClient(ssid="ssid-secret")

    with pytest.raises(Exception, match="not connected"):
        await client.buy("EURUSD_otc", Decimal("1"), 60)


def test_check_win_timeout_keeps_short_expirations_at_default_floor() -> None:
    assert _check_win_timeout(None) == 60
    assert _check_win_timeout(15) == 60
    assert _check_win_timeout(300) == 330
