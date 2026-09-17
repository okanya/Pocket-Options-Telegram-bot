from decimal import Decimal

import pytest

from app.trading.pocket_service import PocketConnectionService, _sanitize_error


class FakeSessionFactory:
    pass


class StubPocketConnectionService(PocketConnectionService):
    def __init__(self, ssid, client_factory):
        super().__init__(FakeSessionFactory(), client_factory=client_factory)
        self.ssid = ssid
        self.events = []
        self.balances = []

    async def _load_ssid(self):
        return self.ssid

    async def _record_event(self, level, event_type, message, error):
        self.events.append((level, event_type, message, error))

    async def _record_balance(self, balance, source):
        self.balances.append((balance, source))


class FakePocketClient:
    created_count = 0

    def __init__(self, ssid):
        type(self).created_count += 1
        self.ssid = ssid
        self.mocked = False
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def get_balance(self):
        return Decimal("123.45")

    async def get_payouts(self):
        return {"EURUSD_otc": Decimal("0.92"), "GBPUSD_otc": Decimal("0.51")}

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.disconnected = True


class FailingPocketClient(FakePocketClient):
    async def connect(self):
        raise RuntimeError(f"bad ssid {self.ssid}")


class FakeConnectedClient(FakePocketClient):
    async def get_balance(self):
        return Decimal("777.00")


class FakeConnectedClientProvider:
    def __init__(self, client):
        self.client = client

    def current_client(self):
        return self.client


@pytest.mark.asyncio
async def test_balance_without_ssid_returns_mocked_status() -> None:
    service = StubPocketConnectionService(ssid=None, client_factory=FakePocketClient)

    result = await service.get_balance()

    assert result.balance is None
    assert result.status.ssid_configured is False
    assert result.status.mocked is True


@pytest.mark.asyncio
async def test_balance_uses_ssid_from_loader() -> None:
    service = StubPocketConnectionService(ssid="ssid-secret", client_factory=FakePocketClient)

    result = await service.get_balance()

    assert result.balance == Decimal("123.45")
    assert result.status.ssid_configured is True
    assert result.status.connected is True
    assert result.status.mocked is False
    assert service.balances == [(Decimal("123.45"), "manual")]


@pytest.mark.asyncio
async def test_balance_uses_active_engine_client_before_opening_new_connection() -> None:
    active_client = FakeConnectedClient("ssid-secret")
    await active_client.connect()
    FakePocketClient.created_count = 0
    service = StubPocketConnectionService(ssid="ssid-secret", client_factory=FakePocketClient)
    service.connected_client_provider = FakeConnectedClientProvider(active_client)

    result = await service.get_balance()

    assert result.balance == Decimal("777.00")
    assert result.status.connected is True
    assert result.status.mocked is False
    assert FakePocketClient.created_count == 0
    assert service.balances == [(Decimal("777.00"), "manual")]


@pytest.mark.asyncio
async def test_balance_error_is_sanitized_and_recorded() -> None:
    service = StubPocketConnectionService(ssid="ssid-secret", client_factory=FailingPocketClient)

    result = await service.get_balance()

    assert result.balance is None
    assert result.status.error == "bad ssid ***"
    assert service.events == [
        ("error", "pocket_option_balance_error", "PocketOption balance request failed", "bad ssid ***")
    ]


@pytest.mark.asyncio
async def test_assets_without_ssid_returns_mocked_status() -> None:
    service = StubPocketConnectionService(ssid=None, client_factory=FakePocketClient)

    result = await service.get_assets()

    assert result.payouts == {}
    assert result.status.ssid_configured is False
    assert result.status.mocked is True


@pytest.mark.asyncio
async def test_assets_uses_ssid_from_loader() -> None:
    service = StubPocketConnectionService(ssid="ssid-secret", client_factory=FakePocketClient)

    result = await service.get_assets()

    assert result.payouts == {"EURUSD_otc": Decimal("0.92"), "GBPUSD_otc": Decimal("0.51")}
    assert result.status.ssid_configured is True
    assert result.status.connected is True
    assert result.status.mocked is False


@pytest.mark.asyncio
async def test_assets_error_is_sanitized_and_recorded() -> None:
    service = StubPocketConnectionService(ssid="ssid-secret", client_factory=FailingPocketClient)

    result = await service.get_assets()

    assert result.payouts == {}
    assert result.status.error == "bad ssid ***"
    assert service.events == [
        ("error", "pocket_option_assets_error", "PocketOption assets request failed", "bad ssid ***")
    ]


def test_sanitize_error_masks_secret() -> None:
    assert _sanitize_error(RuntimeError("ssid-secret failed"), "ssid-secret") == "*** failed"
