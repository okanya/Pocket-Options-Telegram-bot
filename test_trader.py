from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.trading.strategies.base import StrategySignal
import pytest

from app.trading.trader import BrokerTrader, PaperTrader


def test_paper_trader_opens_trade_as_paper() -> None:
    opened_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    trade = PaperTrader().open(
        signal=_signal("buy"),
        amount=Decimal("10"),
        open_price=Decimal("1.1000"),
        payout=Decimal("0.8"),
        opened_at=opened_at,
    )

    assert trade.mode == "paper"
    assert trade.status == "opened"
    assert trade.open_price == Decimal("1.1000")


def test_paper_trader_marks_trade_as_paper_and_settles_buy_win() -> None:
    opened_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    trade = PaperTrader().settle(
        signal=_signal("buy"),
        amount=Decimal("10"),
        open_price=Decimal("1.1000"),
        close_price=Decimal("1.1005"),
        payout=Decimal("0.8"),
        opened_at=opened_at,
        closed_at=opened_at + timedelta(seconds=60),
    )

    assert trade.mode == "paper"
    assert trade.status == "closed"
    assert trade.result == "win"
    assert trade.profit == Decimal("8.0")


def test_paper_trader_settles_sell_win() -> None:
    opened_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    trade = PaperTrader().settle(
        signal=_signal("sell"),
        amount=Decimal("10"),
        open_price=Decimal("1.1000"),
        close_price=Decimal("1.0990"),
        payout=Decimal("0.8"),
        opened_at=opened_at,
        closed_at=opened_at + timedelta(seconds=60),
    )

    assert trade.result == "win"
    assert trade.profit == Decimal("8.0")


def test_paper_trader_settles_loss_and_draw() -> None:
    opened_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    trader = PaperTrader()

    loss = trader.settle(
        signal=_signal("buy"),
        amount=Decimal("10"),
        open_price=Decimal("1.1000"),
        close_price=Decimal("1.0990"),
        payout=Decimal("0.8"),
        opened_at=opened_at,
        closed_at=opened_at + timedelta(seconds=60),
    )
    draw = trader.settle(
        signal=_signal("buy"),
        amount=Decimal("10"),
        open_price=Decimal("1.1000"),
        close_price=Decimal("1.1000"),
        payout=Decimal("0.8"),
        opened_at=opened_at,
        closed_at=opened_at + timedelta(seconds=60),
    )

    assert loss.result == "loss"
    assert loss.profit == Decimal("-10")
    assert draw.result == "draw"
    assert draw.profit == Decimal("0")


@pytest.mark.asyncio
async def test_broker_trader_opens_buy_trade() -> None:
    opened_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    client = FakeBrokerClient()

    trade = await BrokerTrader(mode="demo").open(
        client=client,
        signal=_signal("buy"),
        amount=Decimal("10"),
        payout=Decimal("0.8"),
        opened_at=opened_at,
    )

    assert client.orders == [("buy", "EURUSD_otc", Decimal("10"), 60)]
    assert trade.mode == "demo"
    assert trade.external_trade_id == "trade-1"
    assert trade.open_price == Decimal("1.10")
    assert trade.status == "opened"


@pytest.mark.asyncio
async def test_broker_trader_settles_trade_result() -> None:
    opened_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    client = FakeBrokerClient()
    trader = BrokerTrader(mode="demo")
    open_trade = await trader.open(
        client=client,
        signal=_signal("sell"),
        amount=Decimal("10"),
        payout=Decimal("0.8"),
        opened_at=opened_at,
    )

    result = await trader.settle(client, open_trade, closed_at=opened_at + timedelta(seconds=60))

    assert client.checked_trade_ids == [("trade-1", 60)]
    assert result.mode == "demo"
    assert result.result == "win"
    assert result.profit == Decimal("8")
    assert result.close_price == Decimal("1.09")
    assert result.status == "closed"


def _signal(direction: str) -> StrategySignal:
    return StrategySignal(
        asset="EURUSD_otc",
        strategy_name="trend_pullback",
        direction=direction,
        confidence=1,
        reason={},
        candle_timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        expiration_seconds=60,
    )


class FakeBrokerClient:
    def __init__(self) -> None:
        self.orders = []
        self.checked_trade_ids = []

    async def buy(self, asset, amount, expiration_seconds):
        self.orders.append(("buy", asset, amount, expiration_seconds))
        return "trade-1", {"price": "1.10"}

    async def sell(self, asset, amount, expiration_seconds):
        self.orders.append(("sell", asset, amount, expiration_seconds))
        return "trade-1", {"price": "1.10"}

    async def check_win(self, trade_id, expiration_seconds=None):
        self.checked_trade_ids.append((trade_id, expiration_seconds))
        return {"result": "win", "profit": "8", "close_price": "1.09"}
