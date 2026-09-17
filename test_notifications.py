from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.telegram.notifications import TelegramNotifier
from app.trading.strategies.base import StrategySignal
from app.trading.trader import BrokerTradeResult, OpenBrokerTrade, PaperTrader


class FakeTradeMessageStore:
    def __init__(self):
        self.refs = {}
        self.deleted = []
        self.closed = set()

    async def save(self, external_trade_id, ref):
        self.refs[external_trade_id] = ref

    async def get(self, external_trade_id):
        return self.refs.get(external_trade_id)

    async def delete(self, external_trade_id):
        self.deleted.append(external_trade_id)
        self.refs.pop(external_trade_id, None)

    async def mark_closed(self, external_trade_id):
        self.closed.add(external_trade_id)

    async def is_closed(self, external_trade_id):
        return external_trade_id in self.closed


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeMessage:
    def __init__(self, chat_id, message_id, text):
        self.chat = FakeChat(chat_id)
        self.message_id = message_id
        self.text = text


class FakeBot:
    def __init__(self):
        self.messages = []
        self.edits = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))
        return FakeMessage(chat_id, len(self.messages), text)

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        return FakeMessage(chat_id, message_id, text)


@pytest.mark.asyncio
async def test_telegram_notifier_skips_when_chat_not_configured() -> None:
    bot = FakeBot()
    notifier = TelegramNotifier(bot, Settings(_env_file=None, telegram_notify_chat_id=None))

    await notifier.send("hello")

    assert bot.messages == []


@pytest.mark.asyncio
async def test_telegram_notifier_formats_signal_notification() -> None:
    bot = FakeBot()
    notifier = TelegramNotifier(bot, Settings(_env_file=None, telegram_notify_chat_id=123))

    await notifier.new_signal(
        StrategySignal(
            asset="EURUSD_otc",
            strategy_name="trend_pullback",
            direction="buy",
            confidence=0.8,
            reason={},
            candle_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            expiration_seconds=60,
        )
    )

    assert bot.messages == [
        (
            123,
            "🔔 Signal\n"
            "📊 EURUSD_otc | trend_pullback\n"
            "⬆️ BUY\n"
            "🎯 Confidence: 0.80\n"
            "⏱ Expiration: 60s",
        )
    ]


@pytest.mark.asyncio
async def test_telegram_notifier_formats_lifecycle_and_connection_events() -> None:
    bot = FakeBot()
    notifier = TelegramNotifier(
        bot,
        Settings(
            _env_file=None,
            telegram_notify_chat_id=123,
            active_assets=["EURUSD_otc", "GBPUSD_otc"],
            active_strategy="trend_pullback",
        ),
    )

    await notifier.bot_started("signal_only")
    await notifier.pocket_connected()
    await notifier.pocket_connection_error("invalid ssid")
    await notifier.reconnect_attempt(2, 4)
    await notifier.bot_stopped()

    assert bot.messages == [
        (
            123,
            "Bot started\n"
            "Mode: signal_only\n"
            "Assets: EURUSD_otc, GBPUSD_otc\n"
            "Strategy: trend_pullback",
        ),
        (123, "PocketOption connected"),
        (123, "PocketOption connection error\nError: invalid ssid"),
        (123, "Reconnect attempt\nAttempt: 2\nDelay: 4s"),
        (123, "Bot stopped"),
    ]


@pytest.mark.asyncio
async def test_telegram_notifier_logs_selected_and_rejected_signals_without_sending() -> None:
    bot = FakeBot()
    notifier = TelegramNotifier(bot, Settings(_env_file=None, telegram_notify_chat_id=123))
    signal = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="trend_pullback",
        direction="buy",
        confidence=0.8,
        reason={},
        candle_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        expiration_seconds=60,
    )

    await notifier.signal_selected(signal)
    await notifier.signal_rejected(signal, "daily_stop_loss")

    assert bot.messages == []


@pytest.mark.asyncio
async def test_telegram_notifier_formats_paper_trade_notifications() -> None:
    bot = FakeBot()
    notifier = TelegramNotifier(bot, Settings(_env_file=None, telegram_notify_chat_id=123))
    opened_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    signal = StrategySignal(
        asset="EURUSD_otc",
        strategy_name="trend_pullback",
        direction="buy",
        confidence=0.8,
        reason={},
        candle_timestamp=opened_at,
        expiration_seconds=60,
    )
    trader = PaperTrader()
    opened = trader.open(signal, amount=10, open_price=1.1, payout=0.8, opened_at=opened_at)
    closed = trader.settle(
        signal,
        amount=10,
        open_price=1.1,
        close_price=1.2,
        payout=0.8,
        opened_at=opened_at,
        closed_at=opened_at,
    )

    await notifier.paper_trade_opened(opened)
    await notifier.paper_trade_closed(closed)

    assert bot.messages == [
        (
            123,
            "Paper trade opened\n"
            "Asset: EURUSD_otc\n"
            "Strategy: trend_pullback\n"
            "Direction: buy\n"
            "Amount: 10\n"
            "Open price: 1.1\n"
            "Expiration: 60s",
        ),
        (
            123,
            "Paper trade closed\n"
            "Asset: EURUSD_otc\n"
            "Strategy: trend_pullback\n"
            "Direction: buy\n"
            "Result: win\n"
            "Profit: 8.0\n"
            "Open price: 1.1\n"
            "Close price: 1.2",
        ),
    ]


@pytest.mark.asyncio
async def test_telegram_notifier_formats_broker_trade_notifications() -> None:
    bot = FakeBot()
    notifier = TelegramNotifier(bot, Settings(_env_file=None, telegram_notify_chat_id=123))
    opened_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    opened = OpenBrokerTrade(
        mode="demo",
        external_trade_id="trade-1",
        asset="EURUSD_otc",
        strategy_name="trend_pullback",
        direction="buy",
        amount=10,
        expiration_seconds=60,
        open_price=1.1,
        payout=0.8,
        status="opened",
        opened_at=opened_at,
        raw_response={},
    )
    closed = BrokerTradeResult(
        mode="demo",
        external_trade_id="trade-1",
        asset="EURUSD_otc",
        strategy_name="trend_pullback",
        direction="buy",
        amount=10,
        expiration_seconds=60,
        open_price=1.1,
        close_price=1.2,
        result="win",
        profit=8,
        payout=0.8,
        status="closed",
        opened_at=opened_at,
        closed_at=opened_at,
        raw_response={},
    )

    await notifier.trade_opened(opened)
    await notifier.trade_closed(closed)

    assert bot.messages == [
        (
            123,
            "🟢 Opened\n"
            "📊 EURUSD_otc | trend_pullback\n"
            "⬆️ BUY | 🧪 demo\n"
            "💵 Amount: 10\n"
            "⏱ Expiration: 60s\n"
            "🆔 trade-1",
        ),
    ]


@pytest.mark.asyncio
async def test_telegram_notifier_edits_persisted_broker_trade_message_after_restart() -> None:
    bot = FakeBot()
    store = FakeTradeMessageStore()
    opened_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    opened = OpenBrokerTrade(
        mode="demo",
        external_trade_id="trade-1",
        asset="EURUSD_otc",
        strategy_name="trend_pullback",
        direction="buy",
        amount=10,
        expiration_seconds=60,
        open_price=1.1,
        payout=0.8,
        status="opened",
        opened_at=opened_at,
        raw_response={},
    )
    closed = BrokerTradeResult(
        mode="demo",
        external_trade_id="trade-1",
        asset="EURUSD_otc",
        strategy_name="trend_pullback",
        direction="buy",
        amount=10,
        expiration_seconds=60,
        open_price=1.1,
        close_price=1.2,
        result="win",
        profit=8,
        payout=0.8,
        status="closed",
        opened_at=opened_at,
        closed_at=opened_at,
        raw_response={},
    )

    await TelegramNotifier(
        bot,
        Settings(_env_file=None, telegram_notify_chat_id=123),
        trade_message_store=store,
    ).trade_opened(opened)
    await TelegramNotifier(
        bot,
        Settings(_env_file=None, telegram_notify_chat_id=123),
        trade_message_store=store,
    ).trade_closed(closed)

    assert len(bot.messages) == 1
    assert bot.edits == [
        (
            123,
            1,
            "🟢 Opened\n"
            "📊 EURUSD_otc | trend_pullback\n"
            "⬆️ BUY | 🧪 demo\n"
            "💵 Amount: 10\n"
            "⏱ Expiration: 60s\n"
            "🆔 trade-1\n"
            "\n"
            "✅ WIN\n"
            "💰 Profit: +8",
        )
    ]
    assert store.closed == {"trade-1"}


@pytest.mark.asyncio
async def test_telegram_notifier_skips_duplicate_broker_trade_close() -> None:
    bot = FakeBot()
    store = FakeTradeMessageStore()
    notifier = TelegramNotifier(
        bot,
        Settings(_env_file=None, telegram_notify_chat_id=123),
        trade_message_store=store,
    )
    opened_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    opened = OpenBrokerTrade(
        mode="demo",
        external_trade_id="trade-1",
        asset="EURUSD_otc",
        strategy_name="trend_pullback",
        direction="buy",
        amount=10,
        expiration_seconds=60,
        open_price=1.1,
        payout=0.8,
        status="opened",
        opened_at=opened_at,
        raw_response={},
    )
    closed = BrokerTradeResult(
        mode="demo",
        external_trade_id="trade-1",
        asset="EURUSD_otc",
        strategy_name="trend_pullback",
        direction="buy",
        amount=10,
        expiration_seconds=60,
        open_price=1.1,
        close_price=1.2,
        result="win",
        profit=8,
        payout=0.8,
        status="closed",
        opened_at=opened_at,
        closed_at=opened_at,
        raw_response={},
    )

    await notifier.trade_opened(opened)
    await notifier.trade_closed(closed)
    await notifier.trade_closed(closed)

    assert len(bot.messages) == 1
    assert len(bot.edits) == 1
    assert store.closed == {"trade-1"}


@pytest.mark.asyncio
async def test_telegram_notifier_skips_duplicate_broker_trade_close_after_restart() -> None:
    bot = FakeBot()
    store = FakeTradeMessageStore()
    opened_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    closed = BrokerTradeResult(
        mode="demo",
        external_trade_id="trade-1",
        asset="EURUSD_otc",
        strategy_name="trend_pullback",
        direction="buy",
        amount=10,
        expiration_seconds=60,
        open_price=1.1,
        close_price=1.2,
        result="win",
        profit=8,
        payout=0.8,
        status="closed",
        opened_at=opened_at,
        closed_at=opened_at,
        raw_response={},
    )
    await store.mark_closed("trade-1")

    await TelegramNotifier(
        bot,
        Settings(_env_file=None, telegram_notify_chat_id=123),
        trade_message_store=store,
    ).trade_closed(closed)

    assert bot.messages == []
    assert bot.edits == []
