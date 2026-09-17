import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.config import Settings
from app.trading.confidence import ConfidenceScore
from app.trading import engine as engine_module
from app.trading.candles import CandleData
from app.trading.engine import PendingBrokerTrade, PendingPaperTrade, SignalOnlyEngineRunner, TradingEngine, resolve_signal_expiration
from app.trading.strategies.base import StrategySignal
from app.trading.trader import OpenBrokerTrade


class FakeStorage:
    def __init__(self):
        self.candles = []
        self.signals = []
        self.opened_trades = []
        self.closed_trades = []
        self.loaded_open_trades = []
        self.loaded_open_broker_trades = []
        self.loaded_recent_candles = []

    async def save_candle(self, candle):
        self.candles.append(candle)

    async def load_recent_candles(self, assets, timeframe_seconds, limit):
        return self.loaded_recent_candles

    async def save_signal(self, signal, candle, settings):
        self.signals.append((signal, candle, settings.safe_mode_name))
        return len(self.signals)

    async def open_paper_trade(self, trade, signal_id=None):
        self.opened_trades.append((trade, signal_id))
        return len(self.opened_trades)

    async def close_paper_trade(self, trade_id, trade, signal_id=None):
        self.closed_trades.append((trade_id, trade, signal_id))

    async def load_open_paper_trades(self):
        return self.loaded_open_trades

    async def load_open_broker_trades(self):
        return self.loaded_open_broker_trades

    async def open_broker_trade(self, trade, signal_id=None):
        self.opened_trades.append((trade, signal_id))
        return len(self.opened_trades)

    async def close_broker_trade(self, trade_id, trade, signal_id=None):
        self.closed_trades.append((trade_id, trade, signal_id))


class FakeNotifier:
    def __init__(self):
        self.signals = []
        self.selected = []
        self.rejected = []
        self.opened = []
        self.closed = []
        self.events = []

    async def bot_started(self, mode):
        self.events.append(("bot_started", mode))

    async def bot_stopped(self):
        self.events.append(("bot_stopped",))

    async def pocket_connected(self):
        self.events.append(("pocket_connected",))

    async def pocket_connection_error(self, error):
        self.events.append(("pocket_connection_error", error))

    async def reconnect_attempt(self, attempt, delay_seconds):
        self.events.append(("reconnect_attempt", attempt, delay_seconds))

    async def new_signal(self, signal):
        self.signals.append(signal)

    async def signal_selected(self, signal):
        self.selected.append(signal)

    async def signal_rejected(self, signal, reason):
        self.rejected.append((signal, reason))

    async def paper_trade_opened(self, trade):
        self.opened.append(trade)

    async def paper_trade_closed(self, trade):
        self.closed.append(trade)

    async def trade_opened(self, trade):
        self.opened.append(trade)

    async def trade_closed(self, trade):
        self.closed.append(trade)


class FakeStrategy:
    name = "fake"
    min_candles = 1

    def __init__(self, signal=None):
        self.signal = signal
        self.calls = []

    def generate_signal(self, asset, candles, settings):
        self.calls.append((asset, candles, settings))
        return self.signal


class OneShotStrategy(FakeStrategy):
    def generate_signal(self, asset, candles, settings):
        self.calls.append((asset, candles, settings))
        signal = self.signal
        self.signal = None
        return signal


class NamedFakeStrategy(FakeStrategy):
    def __init__(self, name, signal=None):
        super().__init__(signal=signal)
        self.name = name


class AssetSignalStrategy:
    name = "asset_signal"
    min_candles = 1

    def generate_signal(self, asset, candles, settings):
        return StrategySignal(
            asset=asset,
            strategy_name=self.name,
            direction="buy",
            confidence=1,
            reason={"ok": True},
            candle_timestamp=candles[-1].timestamp,
            expiration_seconds=settings.expiration_seconds,
        )


class FakeConfidenceScorer:
    async def score(self, signal, now):
        return ConfidenceScore(
            value=0.73,
            source="historical_exact",
            sample_size=42,
            win_rate=Decimal("0.55"),
            base_confidence=signal.confidence,
        )


@pytest.mark.asyncio
async def test_engine_ignores_open_or_invalid_candle_payload() -> None:
    storage = FakeStorage()
    strategy = FakeStrategy()
    engine = TradingEngine(settings=Settings(_env_file=None), storage=storage, strategy=strategy)

    signal = await engine.handle_candle_payload("EURUSD_otc", 60, {"closed": False})

    assert signal is None
    assert storage.candles == []
    assert strategy.calls == []


@pytest.mark.asyncio
async def test_engine_saves_closed_candle_without_signal() -> None:
    storage = FakeStorage()
    strategy = FakeStrategy()
    engine = TradingEngine(settings=Settings(_env_file=None), storage=storage, strategy=strategy)

    signal = await engine.handle_candle_payload("EURUSD_otc", 60, _payload())

    assert signal is None
    assert len(storage.candles) == 1
    assert storage.signals == []
    assert len(strategy.calls[0][1]) == 1


@pytest.mark.asyncio
async def test_engine_saves_and_notifies_signal_only_signal() -> None:
    expected = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="buy",
        confidence=1,
        reason={"ok": True},
        candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        expiration_seconds=60,
    )
    storage = FakeStorage()
    notifier = FakeNotifier()
    strategy = FakeStrategy(signal=expected)
    engine = TradingEngine(settings=Settings(_env_file=None), storage=storage, strategy=strategy, notifier=notifier)

    signal = await engine.handle_candle_payload("EURUSD_otc", 60, _payload())

    assert signal == expected
    assert storage.signals[0][0] == expected
    assert storage.signals[0][2] == "signal_only"
    assert notifier.signals == [expected]


@pytest.mark.asyncio
async def test_engine_all_strategy_mode_saves_every_signal() -> None:
    first = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="first",
        direction="buy",
        confidence=1,
        reason={"ok": True},
        candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        expiration_seconds=60,
    )
    second = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="second",
        direction="sell",
        confidence=0.8,
        reason={"ok": True},
        candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        expiration_seconds=60,
    )
    storage = FakeStorage()
    notifier = FakeNotifier()
    engine = TradingEngine(
        settings=Settings(_env_file=None, active_strategy="all"),
        storage=storage,
        strategy=NamedFakeStrategy("first", first),
        strategies=[NamedFakeStrategy("first", first), NamedFakeStrategy("second", second)],
        notifier=notifier,
    )

    signal = await engine.handle_candle_payload("EURUSD_otc", 60, _payload())

    assert signal == first
    assert [saved[0] for saved in storage.signals] == [first, second]
    assert notifier.signals == [first]


@pytest.mark.asyncio
async def test_engine_throttles_signal_only_notifications_per_asset() -> None:
    first = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="buy",
        confidence=1,
        reason={"ok": True},
        candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        expiration_seconds=60,
    )
    second = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="sell",
        confidence=1,
        reason={"ok": True},
        candle_timestamp=datetime.fromtimestamp(1_700_000_060, tz=UTC),
        expiration_seconds=60,
    )
    storage = FakeStorage()
    notifier = FakeNotifier()
    strategy = FakeStrategy(signal=first)
    engine = TradingEngine(settings=Settings(_env_file=None), storage=storage, strategy=strategy, notifier=notifier)

    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_000))
    strategy.signal = second
    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_060))

    assert [saved[0] for saved in storage.signals] == [first, second]
    assert notifier.signals == [first]


@pytest.mark.asyncio
async def test_engine_applies_historical_confidence_before_saving_signal() -> None:
    storage = FakeStorage()
    signal = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="buy",
        confidence=1,
        reason={"conditions": {"ok": True}},
        candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        expiration_seconds=60,
    )
    engine = TradingEngine(
        settings=Settings(_env_file=None),
        storage=storage,
        strategy=FakeStrategy(signal=signal),
        confidence_scorer=FakeConfidenceScorer(),
    )

    await engine.handle_candle_payload("EURUSD_otc", 60, _payload())

    saved_signal = storage.signals[0][0]
    assert saved_signal.confidence == 0.73
    assert saved_signal.reason["confidence"] == {
        "source": "historical_exact",
        "sample_size": 42,
        "win_rate": "0.55",
        "base_confidence": 1.0,
    }


def test_engine_status_defaults_to_safe_mode() -> None:
    engine = TradingEngine(settings=Settings(_env_file=None), storage=FakeStorage())

    assert engine.status().mode == "signal_only"
    assert engine.status().open_trades == 0


def test_engine_strategy_can_be_replaced_at_runtime() -> None:
    first = FakeStrategy()
    second = FakeStrategy()
    engine = TradingEngine(settings=Settings(_env_file=None), storage=FakeStorage(), strategy=first)

    engine.set_strategy(second)

    assert engine.strategy is second


def test_resolve_signal_expiration_uses_fixed_mode() -> None:
    settings = Settings(_env_file=None, expiration_mode="fixed", expiration_seconds=45, timeframe_seconds=300)

    assert resolve_signal_expiration(settings, "breakout_retest") == 45


def test_resolve_signal_expiration_uses_strategy_multiplier_in_auto_mode() -> None:
    settings = Settings(_env_file=None, expiration_mode="auto", timeframe_seconds=300)

    assert resolve_signal_expiration(settings, "bollinger_mean_reversion") == 600
    assert resolve_signal_expiration(settings, "trend_pullback") == 900
    assert resolve_signal_expiration(settings, "breakout_retest") == 1800


@pytest.mark.asyncio
async def test_engine_passes_auto_expiration_to_each_strategy() -> None:
    storage = FakeStorage()
    first = NamedFakeStrategy(
        "trend_pullback",
        StrategySignal(
            asset="EURUSD_otc",
            strategy_name="trend_pullback",
            direction="buy",
            confidence=1,
            reason={"ok": True},
            candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
            expiration_seconds=900,
        ),
    )
    second = NamedFakeStrategy(
        "breakout_retest",
        StrategySignal(
            asset="EURUSD_otc",
            strategy_name="breakout_retest",
            direction="buy",
            confidence=1,
            reason={"ok": True},
            candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
            expiration_seconds=1800,
        ),
    )
    engine = TradingEngine(
        settings=Settings(_env_file=None, active_strategy="all", expiration_mode="auto", timeframe_seconds=300),
        storage=storage,
        strategies=[first, second],
    )

    await engine.handle_candle_payload("EURUSD_otc", 300, _payload(timestamp=1_700_000_000))

    assert first.calls[0][2].expiration_seconds == 900
    assert second.calls[0][2].expiration_seconds == 1800


def test_runner_updates_settings_and_active_engine_strategy() -> None:
    runner = StubSignalOnlyEngineRunner(ssid="ssid")
    engine = TradingEngine(settings=runner.settings, storage=FakeStorage(), strategy=FakeStrategy())
    runner._engine = engine

    runner.set_strategy("breakout_retest")

    assert runner.settings.active_strategy == "breakout_retest"
    assert engine.strategy.name == "breakout_retest"


def test_runner_switches_to_all_strategies_at_runtime() -> None:
    runner = StubSignalOnlyEngineRunner(ssid="ssid")
    engine = TradingEngine(settings=runner.settings, storage=FakeStorage(), strategy=FakeStrategy())
    runner._engine = engine

    runner.set_strategy("all")

    assert runner.settings.active_strategy == "all"
    assert [strategy.name for strategy in engine.strategies] == [
        "trend_pullback",
        "bollinger_mean_reversion",
        "breakout_retest",
    ]


@pytest.mark.asyncio
async def test_engine_restores_open_paper_trades_on_start() -> None:
    expected = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="buy",
        confidence=1,
        reason={"ok": True},
        candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        expiration_seconds=60,
    )
    storage = FakeStorage()
    storage.loaded_open_trades = [
        PendingPaperTrade(
            trade_id=10,
            signal=expected,
            signal_id=1,
            amount=Decimal("10"),
            open_price=Decimal("1.10"),
            payout=Decimal("0.8"),
            opened_at=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        )
    ]
    engine = TradingEngine(
        settings=Settings(_env_file=None, signal_only=False, paper_trading=True),
        storage=storage,
        strategy=OneShotStrategy(signal=None),
    )

    await engine.start()
    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_060, close="1.20"))

    assert len(storage.closed_trades) == 1
    trade_id, trade, signal_id = storage.closed_trades[0]
    assert trade_id == 10
    assert signal_id == 1
    assert trade.result == "win"
    assert engine.status().open_trades == 0


@pytest.mark.asyncio
async def test_engine_preloads_recent_candles_on_start() -> None:
    storage = FakeStorage()
    storage.loaded_recent_candles = [
        _candle("EURUSD_otc", timestamp=1_700_000_000, close="1.10"),
        _candle("EURUSD_otc", timestamp=1_700_000_060, close="1.20"),
    ]
    engine = TradingEngine(settings=Settings(_env_file=None, active_assets=["EURUSD_otc"]), storage=storage)

    await engine.start()

    assert [candle.close for candle in engine.candle_buffer.latest("EURUSD_otc", 60)] == [Decimal("1.10"), Decimal("1.20")]


@pytest.mark.asyncio
async def test_engine_restores_open_broker_trades_on_start() -> None:
    signal = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="buy",
        confidence=1,
        reason={"ok": True},
        candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        expiration_seconds=60,
    )
    storage = FakeStorage()
    storage.loaded_open_broker_trades = [
        PendingBrokerTrade(
            trade_id=20,
            signal=signal,
            signal_id=2,
            open_trade=OpenBrokerTrade(
                mode="demo",
                external_trade_id="trade-1",
                asset="EURUSD_otc",
                strategy_name="fake",
                direction="buy",
                amount=Decimal("10"),
                expiration_seconds=60,
                open_price=Decimal("1.10"),
                payout=Decimal("0.8"),
                status="opened",
                opened_at=datetime.fromtimestamp(1_700_000_000, tz=UTC),
                raw_response={},
            ),
        )
    ]
    engine = TradingEngine(
        settings=Settings(_env_file=None, signal_only=False, paper_trading=False, trading_enabled=True, qt_account_mode="demo"),
        storage=storage,
        strategy=OneShotStrategy(signal=None),
    )

    await engine.start()

    assert engine.status().open_trades == 1


@pytest.mark.asyncio
async def test_engine_opens_and_settles_paper_trade() -> None:
    expected = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="buy",
        confidence=1,
        reason={"ok": True},
        candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        expiration_seconds=60,
    )
    storage = FakeStorage()
    notifier = FakeNotifier()
    strategy = OneShotStrategy(signal=expected)
    engine = TradingEngine(
        settings=Settings(
            _env_file=None,
            signal_only=False,
            paper_trading=True,
            trade_amount="10",
            min_payout="0.8",
        ),
        storage=storage,
        strategy=strategy,
        notifier=notifier,
    )

    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_000, close="1.10"))
    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_060, close="1.20"))

    assert len(storage.opened_trades) == 1
    opened_trade, opened_signal_id = storage.opened_trades[0]
    assert opened_signal_id == 1
    assert opened_trade.mode == "paper"
    assert opened_trade.status == "opened"
    assert engine.status().open_trades == 0
    assert notifier.selected == [expected]

    assert len(storage.closed_trades) == 1
    trade_id, trade, signal_id = storage.closed_trades[0]
    assert trade_id == 1
    assert signal_id == 1
    assert trade.mode == "paper"
    assert trade.result == "win"
    assert trade.profit == 8
    assert len(notifier.opened) == 1
    assert len(notifier.closed) == 1


@pytest.mark.asyncio
async def test_engine_opens_and_settles_qt_demo_trade() -> None:
    expected = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="buy",
        confidence=1,
        reason={"ok": True},
        candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        expiration_seconds=60,
    )
    storage = FakeStorage()
    notifier = FakeNotifier()
    strategy = OneShotStrategy(signal=expected)
    engine = TradingEngine(
        settings=Settings(
            _env_file=None,
            signal_only=False,
            paper_trading=False,
            trading_enabled=True,
            qt_account_mode="demo",
            trade_amount="10",
            min_payout="0.8",
        ),
        storage=storage,
        strategy=strategy,
        notifier=notifier,
    )
    client = FakeMarketDataClient()

    await client.connect()
    engine._client = client
    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_000, close="1.10"))
    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_060, close="1.20"))

    assert client.orders == [("buy", "EURUSD_otc", Decimal("10"), 60)]
    assert client.checked_trade_ids == ["trade-1"]
    assert notifier.selected == [expected]
    assert len(storage.opened_trades) == 1
    opened_trade, opened_signal_id = storage.opened_trades[0]
    assert opened_signal_id == 1
    assert opened_trade.mode == "demo"
    assert opened_trade.external_trade_id == "trade-1"

    assert len(storage.closed_trades) == 1
    trade_id, trade, signal_id = storage.closed_trades[0]
    assert trade_id == 1
    assert signal_id == 1
    assert trade.mode == "demo"
    assert trade.result == "win"
    assert trade.profit == Decimal("8")
    assert len(notifier.opened) == 1
    assert len(notifier.closed) == 1


@pytest.mark.asyncio
async def test_engine_refreshes_runtime_trade_amount_before_broker_trade() -> None:
    expected = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="buy",
        confidence=1,
        reason={"ok": True},
        candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        expiration_seconds=60,
    )

    async def load_runtime_settings(settings):
        return settings.model_copy(update={"trade_amount": Decimal("10")})

    storage = FakeStorage()
    engine = TradingEngine(
        settings=Settings(
            _env_file=None,
            signal_only=False,
            paper_trading=False,
            trading_enabled=True,
            qt_account_mode="demo",
            trade_amount="1",
            min_payout="0.8",
        ),
        storage=storage,
        strategy=OneShotStrategy(signal=expected),
        runtime_settings_loader=load_runtime_settings,
    )
    client = FakeMarketDataClient()

    await client.connect()
    engine._client = client
    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_000, close="1.10"))

    assert client.orders == [("buy", "EURUSD_otc", Decimal("10"), 60)]
    opened_trade, _ = storage.opened_trades[0]
    assert opened_trade.amount == Decimal("10")


@pytest.mark.asyncio
async def test_engine_closes_demo_trade_with_candle_fallback_when_settlement_times_out() -> None:
    expected = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="buy",
        confidence=1,
        reason={"ok": True},
        candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        expiration_seconds=60,
    )
    storage = FakeStorage()
    strategy = OneShotStrategy(signal=expected)
    engine = TradingEngine(
        settings=Settings(
            _env_file=None,
            signal_only=False,
            paper_trading=False,
            trading_enabled=True,
            qt_account_mode="demo",
            trade_amount="10",
            min_payout="0.8",
        ),
        storage=storage,
        strategy=strategy,
    )

    class TimeoutMarketDataClient(FakeMarketDataClient):
        async def check_win(self, trade_id, expiration_seconds=None):
            self.checked_trade_ids.append(trade_id)
            raise TimeoutError("check timeout")

    client = TimeoutMarketDataClient()
    await client.connect()
    engine._client = client

    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_000, close="1.10"))
    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_060, close="1.20"))

    assert client.checked_trade_ids == ["trade-1"]
    assert len(storage.closed_trades) == 1
    _, trade, _ = storage.closed_trades[0]
    assert trade.status == "closed"
    assert trade.result == "win"
    assert trade.close_price == Decimal("1.20")
    assert trade.raw_response["source"] == "candle_fallback"
    assert engine.status().open_trades == 0


@pytest.mark.asyncio
async def test_engine_keeps_real_broker_trade_pending_when_settlement_times_out() -> None:
    expected = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="buy",
        confidence=1,
        reason={"ok": True},
        candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        expiration_seconds=60,
    )
    storage = FakeStorage()
    strategy = OneShotStrategy(signal=expected)
    engine = TradingEngine(
        settings=Settings(
            _env_file=None,
            signal_only=False,
            paper_trading=False,
            trading_enabled=True,
            qt_account_mode="real",
            real_trading_unlocked=True,
            real_trading_admin_confirmed=True,
            trade_amount="10",
            min_payout="0.8",
        ),
        storage=storage,
        strategy=strategy,
    )

    class TimeoutMarketDataClient(FakeMarketDataClient):
        async def check_win(self, trade_id, expiration_seconds=None):
            self.checked_trade_ids.append(trade_id)
            raise TimeoutError("check timeout")

    client = TimeoutMarketDataClient()
    await client.connect()
    engine._client = client

    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_000, close="1.10"))
    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_060, close="1.20"))

    assert client.checked_trade_ids == ["trade-1"]
    assert storage.closed_trades == []
    assert engine.status().open_trades == 1


@pytest.mark.asyncio
async def test_engine_marks_overdue_unsettled_demo_trade_failed() -> None:
    opened_at = datetime.fromtimestamp(1_700_000_000, tz=UTC)
    signal = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="buy",
        confidence=1,
        reason={"ok": True},
        candle_timestamp=opened_at,
        expiration_seconds=60,
    )
    open_trade = OpenBrokerTrade(
        mode="demo",
        external_trade_id="trade-1",
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="buy",
        amount=Decimal("10"),
        expiration_seconds=60,
        open_price=None,
        payout=Decimal("0.8"),
        status="opened",
        opened_at=opened_at,
        raw_response={},
    )
    storage = FakeStorage()
    notifier = FakeNotifier()
    engine = TradingEngine(
        settings=Settings(
            _env_file=None,
            signal_only=False,
            paper_trading=False,
            trading_enabled=True,
            qt_account_mode="demo",
        ),
        storage=storage,
        strategy=OneShotStrategy(),
        notifier=notifier,
    )
    engine._pending_broker_trades = [
        PendingBrokerTrade(trade_id=1, signal=signal, signal_id=1, open_trade=open_trade)
    ]

    class TimeoutMarketDataClient(FakeMarketDataClient):
        async def check_win(self, trade_id, expiration_seconds=None):
            self.checked_trade_ids.append(trade_id)
            raise TimeoutError("check timeout")

    client = TimeoutMarketDataClient()
    await client.connect()
    engine._client = client

    await engine._settle_due_broker_trades(
        _candle(timestamp=1_700_000_360 + 300, asset="EURUSD_otc", close="1.20")
    )

    assert len(storage.closed_trades) == 1
    _, trade, _ = storage.closed_trades[0]
    assert trade.status == "failed"
    assert trade.result == "unknown"
    assert trade.raw_response["source"] == "broker_settlement"
    assert notifier.closed == [trade]
    assert engine.status().open_trades == 0


@pytest.mark.asyncio
async def test_engine_closes_demo_trade_with_candle_fallback_when_deal_is_not_found() -> None:
    expected = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="buy",
        confidence=1,
        reason={"ok": True},
        candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        expiration_seconds=60,
    )
    storage = FakeStorage()
    strategy = OneShotStrategy(signal=expected)
    engine = TradingEngine(
        settings=Settings(
            _env_file=None,
            signal_only=False,
            paper_trading=False,
            trading_enabled=True,
            qt_account_mode="demo",
            trade_amount="10",
            min_payout="0.8",
        ),
        storage=storage,
        strategy=strategy,
    )

    class MissingDealMarketDataClient(FakeMarketDataClient):
        async def check_win(self, trade_id, expiration_seconds=None):
            self.checked_trade_ids.append(trade_id)
            raise RuntimeError(f"PocketOptionError, Failed to find deal: {trade_id}")

    client = MissingDealMarketDataClient()
    await client.connect()
    engine._client = client

    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_000, close="1.10"))
    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_060, close="1.20"))

    assert client.checked_trade_ids == ["trade-1"]
    assert len(storage.closed_trades) == 1
    _, trade, _ = storage.closed_trades[0]
    assert trade.status == "closed"
    assert trade.result == "win"
    assert trade.close_price == Decimal("1.20")
    assert trade.profit == Decimal("8")
    assert trade.raw_response["source"] == "candle_fallback"
    assert "Failed to find deal" in trade.raw_response["error"]
    assert engine.status().open_trades == 0


@pytest.mark.asyncio
async def test_engine_marks_real_broker_trade_failed_when_deal_is_not_found() -> None:
    expected = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="buy",
        confidence=1,
        reason={"ok": True},
        candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        expiration_seconds=60,
    )
    storage = FakeStorage()
    strategy = OneShotStrategy(signal=expected)
    engine = TradingEngine(
        settings=Settings(
            _env_file=None,
            signal_only=False,
            paper_trading=False,
            trading_enabled=True,
            qt_account_mode="real",
            real_trading_unlocked=True,
            real_trading_admin_confirmed=True,
            trade_amount="10",
            min_payout="0.8",
        ),
        storage=storage,
        strategy=strategy,
    )

    class MissingDealMarketDataClient(FakeMarketDataClient):
        async def check_win(self, trade_id, expiration_seconds=None):
            self.checked_trade_ids.append(trade_id)
            raise RuntimeError(f"PocketOptionError, Failed to find deal: {trade_id}")

    client = MissingDealMarketDataClient()
    await client.connect()
    engine._client = client

    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_000, close="1.10"))
    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_060, close="1.20"))

    assert client.checked_trade_ids == ["trade-1"]
    assert len(storage.closed_trades) == 1
    _, trade, _ = storage.closed_trades[0]
    assert trade.status == "failed"
    assert trade.result == "unknown"
    assert "Failed to find deal" in trade.raw_response["error"]
    assert engine.status().open_trades == 0


@pytest.mark.asyncio
async def test_engine_opens_and_settles_qt_real_trade_when_unlocked_and_confirmed() -> None:
    expected = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="fake",
        direction="sell",
        confidence=1,
        reason={"ok": True},
        candle_timestamp=datetime.fromtimestamp(1_700_000_000, tz=UTC),
        expiration_seconds=60,
    )
    storage = FakeStorage()
    notifier = FakeNotifier()
    strategy = OneShotStrategy(signal=expected)
    engine = TradingEngine(
        settings=Settings(
            _env_file=None,
            signal_only=False,
            paper_trading=False,
            trading_enabled=True,
            qt_account_mode="real",
            real_trading_unlocked=True,
            real_trading_admin_confirmed=True,
            trade_amount="10",
            min_payout="0.8",
        ),
        storage=storage,
        strategy=strategy,
        notifier=notifier,
    )
    client = FakeMarketDataClient()

    await client.connect()
    engine._client = client
    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_000, close="1.10"))
    await engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_060, close="1.00"))

    assert client.orders == [("sell", "EURUSD_otc", Decimal("10"), 60)]
    assert client.checked_trade_ids == ["trade-1"]
    assert notifier.selected == [expected]
    opened_trade, opened_signal_id = storage.opened_trades[0]
    assert opened_signal_id == 1
    assert opened_trade.mode == "real"
    assert opened_trade.external_trade_id == "trade-1"

    trade_id, trade, signal_id = storage.closed_trades[0]
    assert trade_id == 1
    assert signal_id == 1
    assert trade.mode == "real"
    assert trade.result == "win"
    assert trade.profit == Decimal("8")
    assert len(notifier.opened) == 1
    assert len(notifier.closed) == 1


@pytest.mark.asyncio
async def test_engine_enforces_broker_open_trade_limit_across_concurrent_assets() -> None:
    storage = FakeStorage()
    notifier = FakeNotifier()
    engine = TradingEngine(
        settings=Settings(
            _env_file=None,
            signal_only=False,
            paper_trading=False,
            trading_enabled=True,
            qt_account_mode="demo",
            max_open_trades_total=1,
            max_open_trades_per_asset=1,
        ),
        storage=storage,
        strategy=AssetSignalStrategy(),
        notifier=notifier,
    )
    client = FakeMarketDataClient()
    await client.connect()
    engine._client = client

    await asyncio.gather(
        engine.handle_candle_payload("EURUSD_otc", 60, _payload(timestamp=1_700_000_000, close="1.10")),
        engine.handle_candle_payload("GBPUSD_otc", 60, _payload(timestamp=1_700_000_000, close="1.20")),
    )

    assert len(client.orders) == 1
    assert len(storage.opened_trades) == 1
    assert engine.status().open_trades == 1
    assert [reason for _, reason in notifier.rejected] == ["max_open_trades_total"]


@pytest.mark.asyncio
async def test_engine_consumes_asset_subscriptions() -> None:
    storage = FakeStorage()
    strategy = FakeStrategy()
    engine = TradingEngine(
        settings=Settings(_env_file=None, active_assets=["EURUSD_otc", "GBPUSD_otc"]),
        storage=storage,
        strategy=strategy,
    )
    client = FakeMarketDataClient()

    with pytest.raises(RuntimeError, match="All market data subscriptions stopped"):
        await engine.run_signal_only(client)

    assert client.connected is False
    assert sorted(client.subscribed_assets) == ["EURUSD_otc", "GBPUSD_otc"]
    assert len(storage.candles) == 2


@pytest.mark.asyncio
async def test_engine_reconnects_when_market_data_subscription_is_stale(monkeypatch) -> None:
    monkeypatch.setattr(engine_module, "_market_data_timeout_seconds", lambda timeframe_seconds: 0.01)
    storage = FakeStorage()
    engine = TradingEngine(
        settings=Settings(_env_file=None, active_assets=["EURUSD_otc"]),
        storage=storage,
        strategy=FakeStrategy(),
    )
    client = BlockingMarketDataClient()

    with pytest.raises(RuntimeError, match="Market data subscription stale for EURUSD_otc"):
        await engine.run_signal_only(client)

    assert client.connected is False


@pytest.mark.asyncio
async def test_engine_reconnects_when_no_closed_candles_are_received(monkeypatch) -> None:
    current_time = 1000.0

    def fake_monotonic():
        nonlocal current_time
        current_time += 1.0
        return current_time

    monkeypatch.setattr(engine_module, "_market_data_timeout_seconds", lambda timeframe_seconds: 3)
    monkeypatch.setattr(engine_module.time, "monotonic", fake_monotonic)
    storage = FakeStorage()
    engine = TradingEngine(
        settings=Settings(_env_file=None, active_assets=["EURUSD_otc"]),
        storage=storage,
        strategy=FakeStrategy(),
    )
    client = ServicePayloadMarketDataClient()

    with pytest.raises(RuntimeError, match="Market data candle stream stale for EURUSD_otc"):
        await engine.run_signal_only(client)

    assert client.connected is False
    assert storage.candles == []


@pytest.mark.asyncio
async def test_engine_replaces_asset_subscriptions_at_runtime() -> None:
    storage = FakeStorage()
    engine = TradingEngine(settings=Settings(_env_file=None, active_assets=["EURUSD_otc"]), storage=storage, strategy=FakeStrategy())
    client = BlockingMarketDataClient()

    await client.connect()
    await engine.start()
    engine._client = client
    engine.set_assets(["EURUSD_otc"])
    await _yield_once()

    assert client.subscribed_assets == ["EURUSD_otc"]
    assert len(engine._tasks) == 1

    engine.set_assets(["USDTHB_otc", "EURUSD_otc"])
    await _yield_once()

    assert client.subscribed_assets == ["EURUSD_otc", "USDTHB_otc", "EURUSD_otc"]
    assert sorted(task.get_name() for task in engine._tasks) == ["signal-only:EURUSD_otc", "signal-only:USDTHB_otc"]

    await engine.stop()


@pytest.mark.asyncio
async def test_engine_stops_for_reconnect_when_ssid_changes() -> None:
    fingerprints = iter(["old", "new"])

    async def load_ssid_fingerprint():
        return next(fingerprints)

    storage = FakeStorage()
    engine = TradingEngine(
        settings=Settings(_env_file=None, active_assets=["EURUSD_otc"]),
        storage=storage,
        strategy=FakeStrategy(),
        ssid_fingerprint_loader=load_ssid_fingerprint,
        ssid_refresh_seconds=0,
    )
    client = BlockingMarketDataClient()

    await asyncio.wait_for(engine.run_signal_only(client), timeout=2)

    assert engine.status().running is False
    assert client.connected is False


@pytest.mark.asyncio
async def test_runner_records_missing_ssid_event() -> None:
    runner = StubSignalOnlyEngineRunner(ssid=None)

    await runner.run()

    assert runner.events == [("warning", "pocket_option_ssid_missing", "PocketOption SSID is not configured", None)]


@pytest.mark.asyncio
async def test_runner_does_not_start_when_signal_only_disabled() -> None:
    runner = StubSignalOnlyEngineRunner(
        ssid="ssid-secret",
        settings=Settings(_env_file=None, signal_only=False, trading_enabled=False),
    )

    await runner.run()

    assert runner.events == [
        ("warning", "engine_not_started", "Engine requires signal-only, paper trading, or QT demo trading mode", None)
    ]


@pytest.mark.asyncio
async def test_runner_does_not_start_real_mode_without_admin_confirmation() -> None:
    runner = StubSignalOnlyEngineRunner(
        ssid="ssid-secret",
        settings=Settings(
            _env_file=None,
            signal_only=False,
            trading_enabled=True,
            qt_account_mode="real",
            real_trading_unlocked=True,
        ),
    )

    await runner.run()

    assert runner.events == [
        ("warning", "engine_not_started", "Engine requires signal-only, paper trading, or QT demo trading mode", None)
    ]


@pytest.mark.asyncio
async def test_runner_does_not_start_with_unknown_strategy() -> None:
    runner = StubSignalOnlyEngineRunner(
        ssid="ssid-secret",
        settings=Settings(_env_file=None, active_strategy="unknown"),
    )

    await runner.run()

    assert runner.events == [
        (
            "error",
            "unknown_strategy",
            "Configured strategy is not available",
            {"strategy": "unknown", "available": ["all", "bollinger_mean_reversion", "breakout_retest", "trend_pullback"]},
        )
    ]


@pytest.mark.asyncio
async def test_runner_sanitizes_connection_errors(monkeypatch) -> None:
    notifier = FakeNotifier()
    runner = StubSignalOnlyEngineRunner(
        ssid="ssid-secret",
        notifier=notifier,
        client_factory=FailingMarketDataClient,
        max_reconnect_attempts=1,
    )
    monkeypatch.setattr("app.trading.engine.asyncio.sleep", _no_sleep)

    await runner.run()

    assert runner.events[-1] == (
        "error",
        "signal_engine_error",
        "Trading engine error",
        {"error": "cannot connect with ***", "attempt": 1},
    )
    assert ("pocket_connection_error", "cannot connect with ***") not in notifier.events


@pytest.mark.asyncio
async def test_runner_warns_when_ssid_is_probably_expired(monkeypatch) -> None:
    notifier = FakeNotifier()
    runner = StubSignalOnlyEngineRunner(
        ssid="ssid-secret",
        notifier=notifier,
        client_factory=InitializationTimeoutMarketDataClient,
        max_reconnect_attempts=3,
    )
    monkeypatch.setattr("app.trading.engine.asyncio.sleep", _no_sleep)

    await runner.run()

    assert (
        "warning",
        "pocket_option_ssid_probably_expired",
        "PocketOption SSID may be expired or rejected",
        {"error": "PocketOptionError, General error: Connection initialization timed out", "attempt": 3},
    ) in runner.events
    assert any(
        event[0] == "pocket_connection_error" and "PocketOption SSID may be expired or rejected" in event[1]
        for event in notifier.events
    )


@pytest.mark.asyncio
async def test_runner_reconnects_when_subscriptions_stop() -> None:
    runner = StubSignalOnlyEngineRunner(
        ssid="ssid-secret",
        client_factory=NoPayloadMarketDataClient,
        max_reconnect_attempts=1,
    )

    await runner.run()

    assert runner.events[-1] == (
        "error",
        "signal_engine_error",
        "Trading engine error",
        {"error": "All market data subscriptions stopped", "attempt": 1},
    )


@pytest.mark.asyncio
async def test_runner_notifies_lifecycle_and_pocket_connected() -> None:
    notifier = FakeNotifier()
    runner = StubSignalOnlyEngineRunner(
        ssid="ssid-secret",
        notifier=notifier,
        client_factory=NoPayloadMarketDataClient,
        max_reconnect_attempts=1,
    )

    await runner.run()

    assert notifier.events == [
        ("bot_started", "signal_only"),
        ("pocket_connected",),
        ("bot_stopped",),
    ]


@pytest.mark.asyncio
async def test_runner_suppresses_transient_reconnect_notifications(monkeypatch) -> None:
    notifier = FakeNotifier()
    runner = StubSignalOnlyEngineRunner(
        ssid="ssid-secret",
        notifier=notifier,
        client_factory=FailingMarketDataClient,
        max_reconnect_attempts=2,
        notify_reconnect_after_attempts=5,
    )
    monkeypatch.setattr("app.trading.engine.asyncio.sleep", _no_sleep)

    await runner.run()

    assert ("reconnect_attempt", 2, 0) not in notifier.events
    assert ("pocket_connected",) not in notifier.events[1:]
    assert ("pocket_connection_error", "cannot connect with ***") not in notifier.events


@pytest.mark.asyncio
async def test_runner_does_not_notify_transient_errors_after_reconnect_threshold(monkeypatch) -> None:
    notifier = FakeNotifier()
    runner = StubSignalOnlyEngineRunner(
        ssid="ssid-secret",
        notifier=notifier,
        client_factory=FailingMarketDataClient,
        max_reconnect_attempts=5,
        notify_reconnect_after_attempts=3,
    )
    monkeypatch.setattr("app.trading.engine.asyncio.sleep", _no_sleep)

    await runner.run()

    assert ("pocket_connection_error", "cannot connect with ***") not in notifier.events
    assert not any(event[0] == "reconnect_attempt" for event in notifier.events)


def _payload(timestamp=1_700_000_000, close="1.15"):
    return {
        "time": timestamp,
        "open": "1.1",
        "high": "1.2",
        "low": "1.0",
        "close": close,
    }


def _candle(asset, timestamp=1_700_000_000, close="1.15"):
    return CandleData(
        asset=asset,
        timeframe_seconds=60,
        timestamp=datetime.fromtimestamp(timestamp, tz=UTC),
        open=Decimal("1.1"),
        high=Decimal("1.2"),
        low=Decimal("1.0"),
        close=Decimal(close),
    )


class FakeMarketDataClient:
    def __init__(self, ssid=None):
        self.ssid = ssid
        self.connected = False
        self.subscribed_assets = []

    async def connect(self):
        self.connected = True
        self.orders = []
        self.checked_trade_ids = []

    async def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    async def subscribe_candles(self, asset, timeframe_seconds):
        self.subscribed_assets.append(asset)
        yield _payload()
        yield ["not", "a", "dict"]

    async def buy(self, asset, amount, expiration_seconds):
        self.orders.append(("buy", asset, amount, expiration_seconds))
        return "trade-1", {"price": "1.10"}

    async def sell(self, asset, amount, expiration_seconds):
        self.orders.append(("sell", asset, amount, expiration_seconds))
        return "trade-1", {"price": "1.10"}

    async def check_win(self, trade_id, expiration_seconds=None):
        self.checked_trade_ids.append(trade_id)
        return {"result": "win", "profit": "8", "close_price": "1.20"}

    async def get_payout(self, asset):
        return Decimal("0.8")


class FailingMarketDataClient(FakeMarketDataClient):
    async def connect(self):
        raise RuntimeError(f"cannot connect with {self.ssid}")


class InitializationTimeoutMarketDataClient(FakeMarketDataClient):
    async def connect(self):
        raise RuntimeError("PocketOptionError, General error: Connection initialization timed out")


class NoPayloadMarketDataClient(FakeMarketDataClient):
    async def subscribe_candles(self, asset, timeframe_seconds):
        self.subscribed_assets.append(asset)
        if False:
            yield _payload()


class BlockingMarketDataClient(FakeMarketDataClient):
    async def subscribe_candles(self, asset, timeframe_seconds):
        self.subscribed_assets.append(asset)
        while True:
            await _yield_once()
            if False:
                yield _payload()


class ServicePayloadMarketDataClient(FakeMarketDataClient):
    async def subscribe_candles(self, asset, timeframe_seconds):
        self.subscribed_assets.append(asset)
        while True:
            await _yield_once()
            yield {"type": "heartbeat"}


class StubSignalOnlyEngineRunner(SignalOnlyEngineRunner):
    def __init__(
        self,
        ssid,
        settings=None,
        notifier=None,
        client_factory=FakeMarketDataClient,
        max_reconnect_attempts=None,
        notify_reconnect_after_attempts=5,
    ):
        super().__init__(
            settings=settings or Settings(_env_file=None),
            session_factory=FakeSessionFactory(),
            notifier=notifier,
            client_factory=client_factory,
            max_reconnect_attempts=max_reconnect_attempts,
            initial_backoff_seconds=0,
            notify_reconnect_after_attempts=notify_reconnect_after_attempts,
        )
        self.ssid = ssid
        self.events = []

    async def _load_ssid(self):
        return self.ssid

    async def _record_event(self, level, event_type, message, payload=None):
        self.events.append((level, event_type, message, payload))


class FakeSessionFactory:
    pass


async def _no_sleep(seconds):
    return None


async def _yield_once():
    import asyncio

    await asyncio.sleep(0)
