from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.repositories import BotEventRepository, CandleRepository, SecretSettingsRepository, SignalRepository
from app.runtime_settings import RuntimeSettingsService
from app.trading.confidence import HistoricalConfidenceScorer
from app.trading.candles import CandleBuffer, CandleData, parse_closed_candle
from app.trading.pocket_client import PocketClient
from app.trading.risk import RiskContext, RiskManager
from app.trading.signal_selector import SignalSelector
from app.trading.strategies import UnknownStrategyError, available_strategy_names, create_strategies
from app.trading.strategies.base import BaseStrategy, StrategySignal, StrategySettings
from app.trading.strategies.trend_pullback import TrendPullbackStrategy
from app.trading.trader import BrokerTradeResult, BrokerTrader, OpenBrokerTrade, OpenPaperTrade, PaperTrade, PaperTrader

logger = logging.getLogger(__name__)
BROKER_SETTLEMENT_MAX_OVERDUE_SECONDS = 300
SIGNAL_ONLY_SIGNAL_NOTIFICATION_COOLDOWN_SECONDS = 900


def _market_data_timeout_seconds(timeframe_seconds: int) -> float:
    return float(max(timeframe_seconds * 3, 180))


def _is_probable_ssid_error(error: str) -> bool:
    normalized = error.lower()
    return any(
        marker in normalized
        for marker in (
            "failed to parse ssid",
            "invalid ssid",
            "ssid does not",
            "auth",
        )
    )


def _is_pocket_initialization_timeout(error: str) -> bool:
    return "connection initialization timed out" in error.lower()


def _should_warn_about_probable_expired_ssid(error: str, attempt: int) -> bool:
    if _is_probable_ssid_error(error):
        return True
    if _is_pocket_initialization_timeout(error):
        return attempt == 3 or attempt % 5 == 0
    return False


def _probable_expired_ssid_message(error: str) -> str:
    return (
        "PocketOption SSID may be expired or rejected.\n"
        f"Last error: {error}\n"
        "Action: open PocketOption in browser, refresh the trading page, copy a fresh 42[\"auth\", ...] SSID, "
        "then send /set_ssid <ssid>."
    )


@dataclass(frozen=True)
class EngineStatus:
    running: bool = False
    connected: bool = False
    mode: str = "signal_only"
    open_trades: int = 0


@dataclass(frozen=True)
class PendingPaperTrade:
    trade_id: int | None
    signal: StrategySignal
    signal_id: int | None
    amount: Decimal
    open_price: Decimal
    payout: Decimal
    opened_at: Any


@dataclass(frozen=True)
class PendingBrokerTrade:
    trade_id: int | None
    signal: StrategySignal
    signal_id: int | None
    open_trade: OpenBrokerTrade


class SignalOnlyStorage(Protocol):
    async def save_candle(self, candle: CandleData) -> None:
        ...

    async def load_recent_candles(self, assets: list[str], timeframe_seconds: int, limit: int) -> list[CandleData]:
        ...

    async def save_signal(
        self,
        signal: StrategySignal,
        candle: CandleData,
        settings: Settings,
    ) -> int | None:
        ...

    async def open_paper_trade(self, trade: OpenPaperTrade, signal_id: int | None = None) -> int | None:
        ...

    async def close_paper_trade(self, trade_id: int | None, trade: PaperTrade, signal_id: int | None = None) -> None:
        ...

    async def load_open_paper_trades(self) -> list[PendingPaperTrade]:
        ...

    async def load_open_broker_trades(self) -> list[PendingBrokerTrade]:
        ...

    async def open_broker_trade(self, trade: OpenBrokerTrade, signal_id: int | None = None) -> int | None:
        ...

    async def close_broker_trade(self, trade_id: int | None, trade: BrokerTradeResult, signal_id: int | None = None) -> None:
        ...


class SignalNotifier(Protocol):
    async def bot_started(self, mode: str) -> None:
        ...

    async def bot_stopped(self) -> None:
        ...

    async def pocket_connected(self) -> None:
        ...

    async def pocket_connection_error(self, error: str) -> None:
        ...

    async def reconnect_attempt(self, attempt: int, delay_seconds: float) -> None:
        ...

    async def new_signal(self, signal: StrategySignal) -> None:
        ...

    async def signal_selected(self, signal: StrategySignal) -> None:
        ...

    async def signal_rejected(self, signal: StrategySignal, reason: str) -> None:
        ...

    async def paper_trade_opened(self, trade: OpenPaperTrade) -> None:
        ...

    async def paper_trade_closed(self, trade: PaperTrade) -> None:
        ...

    async def trade_opened(self, trade: OpenBrokerTrade) -> None:
        ...

    async def trade_closed(self, trade: BrokerTradeResult) -> None:
        ...


class NullSignalNotifier:
    async def bot_started(self, mode: str) -> None:
        return None

    async def bot_stopped(self) -> None:
        return None

    async def pocket_connected(self) -> None:
        return None

    async def pocket_connection_error(self, error: str) -> None:
        return None

    async def reconnect_attempt(self, attempt: int, delay_seconds: float) -> None:
        return None

    async def new_signal(self, signal: StrategySignal) -> None:
        return None

    async def signal_selected(self, signal: StrategySignal) -> None:
        return None

    async def signal_rejected(self, signal: StrategySignal, reason: str) -> None:
        return None

    async def paper_trade_opened(self, trade: OpenPaperTrade) -> None:
        return None

    async def paper_trade_closed(self, trade: PaperTrade) -> None:
        return None

    async def trade_opened(self, trade: OpenBrokerTrade) -> None:
        return None

    async def trade_closed(self, trade: BrokerTradeResult) -> None:
        return None


class MarketDataClient(Protocol):
    async def connect(self) -> None:
        ...

    async def disconnect(self) -> None:
        ...

    async def subscribe_candles(self, asset: str, timeframe_seconds: int) -> AsyncIterator[dict[str, Any]]:
        ...

    async def buy(self, asset: str, amount: Decimal, expiration_seconds: int) -> tuple[str, dict[str, Any]]:
        ...

    async def sell(self, asset: str, amount: Decimal, expiration_seconds: int) -> tuple[str, dict[str, Any]]:
        ...

    async def check_win(self, trade_id: str, expiration_seconds: int | None = None) -> dict[str, Any] | None:
        ...

    async def get_payout(self, asset: str) -> Decimal | None:
        ...

    def is_connected(self) -> bool:
        ...


class DatabaseSignalOnlyStorage:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], settings: Settings | None = None) -> None:
        self.session_factory = session_factory
        self.settings = settings

    async def save_candle(self, candle: CandleData) -> None:
        async with self.session_factory() as session:
            repo = CandleRepository(session)
            await repo.upsert(
                asset=candle.asset,
                timeframe_seconds=candle.timeframe_seconds,
                timestamp=candle.timestamp,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                source=candle.source,
                raw_payload=candle.raw_payload,
            )
            await session.commit()

    async def load_recent_candles(self, assets: list[str], timeframe_seconds: int, limit: int) -> list[CandleData]:
        async with self.session_factory() as session:
            repo = CandleRepository(session)
            candles: list[CandleData] = []
            for asset in assets:
                rows = await repo.latest(asset=asset, timeframe_seconds=timeframe_seconds, limit=limit)
                candles.extend(
                    CandleData(
                        asset=row.asset,
                        timeframe_seconds=row.timeframe_seconds,
                        timestamp=row.timestamp,
                        open=row.open,
                        high=row.high,
                        low=row.low,
                        close=row.close,
                        volume=row.volume,
                        source=row.source,
                        raw_payload=row.raw_payload,
                    )
                    for row in rows
                )
            return sorted(candles, key=lambda candle: (candle.asset, candle.timeframe_seconds, candle.timestamp))

    async def save_signal(
        self,
        signal: StrategySignal,
        candle: CandleData,
        settings: Settings,
    ) -> int | None:
        async with self.session_factory() as session:
            repo = SignalRepository(session)
            saved = await repo.create(
                asset=signal.asset,
                strategy_name=signal.strategy_name,
                direction=signal.direction,
                timeframe_seconds=settings.timeframe_seconds,
                expiration_seconds=signal.expiration_seconds,
                price=candle.close,
                candle_timestamp=signal.candle_timestamp,
                confidence=Decimal(str(signal.confidence)) if signal.confidence is not None else None,
                reason=signal.reason,
                mode=settings.safe_mode_name,
                is_selected=False,
                is_trade_allowed=False,
                rejection_reason="signal_only_mode" if settings.signal_only else None,
            )
            await session.commit()
            return saved.id

    async def open_paper_trade(self, trade: OpenPaperTrade, signal_id: int | None = None) -> int | None:
        from app.db.repositories import TradeRepository

        async with self.session_factory() as session:
            repo = TradeRepository(session)
            saved = await repo.create(
                external_trade_id=None,
                signal_id=signal_id,
                asset=trade.asset,
                strategy_name=trade.strategy_name,
                mode=trade.mode,
                direction=trade.direction,
                amount=trade.amount,
                expiration_seconds=trade.expiration_seconds,
                open_price=trade.open_price,
                close_price=None,
                result="unknown",
                profit=None,
                payout=trade.payout,
                status=trade.status,
                opened_at=trade.opened_at,
                closed_at=None,
                raw_response={"source": "paper_trader"},
            )
            await session.commit()
            return saved.id

    async def close_paper_trade(self, trade_id: int | None, trade: PaperTrade, signal_id: int | None = None) -> None:
        from app.db.repositories import TradeRepository

        async with self.session_factory() as session:
            repo = TradeRepository(session)
            values = {
                "close_price": trade.close_price,
                "result": trade.result,
                "profit": trade.profit,
                "status": trade.status,
                "closed_at": trade.closed_at,
                "raw_response": {"source": "paper_trader"},
            }
            if trade_id is not None:
                updated = await repo.update(trade_id, **values)
                if updated is not None:
                    await session.commit()
                    return

            await repo.create(
                external_trade_id=None,
                signal_id=signal_id,
                asset=trade.asset,
                strategy_name=trade.strategy_name,
                mode=trade.mode,
                direction=trade.direction,
                amount=trade.amount,
                expiration_seconds=trade.expiration_seconds,
                open_price=trade.open_price,
                close_price=trade.close_price,
                result=trade.result,
                profit=trade.profit,
                payout=trade.payout,
                status=trade.status,
                opened_at=trade.opened_at,
                closed_at=trade.closed_at,
                raw_response={"source": "paper_trader"},
            )
            await session.commit()

    async def load_open_paper_trades(self) -> list[PendingPaperTrade]:
        from app.db.repositories import TradeRepository

        async with self.session_factory() as session:
            trades = await TradeRepository(session).open_paper_trades()
            pending: list[PendingPaperTrade] = []
            for trade in trades:
                if trade.signal is None:
                    logger.warning("Skipping open paper trade without signal: trade_id=%s", trade.id)
                    continue
                pending.append(
                    PendingPaperTrade(
                        trade_id=trade.id,
                        signal=StrategySignal(
                            asset=trade.signal.asset,
                            strategy_name=trade.signal.strategy_name,
                            direction=trade.signal.direction,
                            confidence=float(trade.signal.confidence) if trade.signal.confidence is not None else None,
                            reason=trade.signal.reason,
                            candle_timestamp=trade.signal.candle_timestamp,
                            expiration_seconds=trade.signal.expiration_seconds,
                        ),
                        signal_id=trade.signal_id,
                        amount=trade.amount,
                        open_price=trade.open_price,
                        payout=trade.payout or (self.settings.min_payout if self.settings else Decimal("0")),
                        opened_at=trade.opened_at,
                    )
                )
            return pending

    async def load_open_broker_trades(self) -> list[PendingBrokerTrade]:
        from app.db.repositories import TradeRepository

        async with self.session_factory() as session:
            repo = TradeRepository(session)
            trades = await repo.open_broker_trades()
            pending: list[PendingBrokerTrade] = []
            for trade in trades:
                signal = _signal_from_trade(trade)
                if signal is None or trade.external_trade_id is None:
                    continue
                open_trade = OpenBrokerTrade(
                    mode=trade.mode,
                    external_trade_id=trade.external_trade_id,
                    asset=trade.asset,
                    strategy_name=trade.strategy_name,
                    direction=trade.direction,
                    amount=trade.amount,
                    expiration_seconds=trade.expiration_seconds,
                    open_price=trade.open_price,
                    payout=trade.payout,
                    status=trade.status,
                    opened_at=trade.opened_at,
                    raw_response=trade.raw_response or {"source": "db_restore"},
                )
                pending.append(
                    PendingBrokerTrade(
                        trade_id=trade.id,
                        signal=signal,
                        signal_id=trade.signal_id,
                        open_trade=open_trade,
                    )
                )
            return pending

    async def open_broker_trade(self, trade: OpenBrokerTrade, signal_id: int | None = None) -> int | None:
        from app.db.repositories import TradeRepository

        async with self.session_factory() as session:
            repo = TradeRepository(session)
            saved = await repo.create(
                external_trade_id=trade.external_trade_id,
                signal_id=signal_id,
                asset=trade.asset,
                strategy_name=trade.strategy_name,
                mode=trade.mode,
                direction=trade.direction,
                amount=trade.amount,
                expiration_seconds=trade.expiration_seconds,
                open_price=trade.open_price,
                close_price=None,
                result="unknown",
                profit=None,
                payout=trade.payout,
                status=trade.status,
                opened_at=trade.opened_at,
                closed_at=None,
                raw_response=trade.raw_response,
            )
            await session.commit()
            return saved.id

    async def close_broker_trade(self, trade_id: int | None, trade: BrokerTradeResult, signal_id: int | None = None) -> None:
        from app.db.repositories import TradeRepository

        async with self.session_factory() as session:
            repo = TradeRepository(session)
            values = {
                "close_price": trade.close_price,
                "result": trade.result,
                "profit": trade.profit,
                "status": trade.status,
                "closed_at": trade.closed_at,
                "raw_response": trade.raw_response,
            }
            if trade_id is not None:
                updated = await repo.update(trade_id, **values)
                if updated is not None:
                    await session.commit()
                    return

            await repo.create(
                external_trade_id=trade.external_trade_id,
                signal_id=signal_id,
                asset=trade.asset,
                strategy_name=trade.strategy_name,
                mode=trade.mode,
                direction=trade.direction,
                amount=trade.amount,
                expiration_seconds=trade.expiration_seconds,
                open_price=trade.open_price,
                close_price=trade.close_price,
                result=trade.result,
                profit=trade.profit,
                payout=trade.payout,
                status=trade.status,
                opened_at=trade.opened_at,
                closed_at=trade.closed_at,
                raw_response=trade.raw_response,
            )
            await session.commit()


@dataclass
class TradingEngine:
    settings: Settings
    storage: SignalOnlyStorage
    strategy: BaseStrategy = field(default_factory=TrendPullbackStrategy)
    strategies: list[BaseStrategy] | None = None
    notifier: SignalNotifier = field(default_factory=NullSignalNotifier)
    candle_buffer: CandleBuffer = field(default_factory=CandleBuffer)
    signal_selector: SignalSelector | None = None
    confidence_scorer: HistoricalConfidenceScorer | None = None
    risk_manager: RiskManager | None = None
    paper_trader: PaperTrader = field(default_factory=PaperTrader)
    broker_trader: BrokerTrader | None = None
    notify_pocket_connected: bool = True
    runtime_settings_loader: Callable[[Settings], Awaitable[Settings]] | None = None
    ssid_fingerprint_loader: Callable[[], Awaitable[str | None]] | None = None
    ssid_refresh_seconds: float = 5.0
    runtime_settings_refresh_seconds: float = 5.0
    _running: bool = False
    _connected: bool = False
    _tasks: set[asyncio.Task] = field(default_factory=set)
    _pending_paper_trades: list[PendingPaperTrade] = field(default_factory=list)
    _pending_broker_trades: list[PendingBrokerTrade] = field(default_factory=list)
    _client: MarketDataClient | None = None
    _last_candle_received_at: dict[str, float] = field(default_factory=dict)
    _last_signal_only_notification_at: dict[str, Any] = field(default_factory=dict)
    _trade_open_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_runtime_settings_refresh: float = 0.0

    def __post_init__(self) -> None:
        if self.strategies is None:
            self.strategies = [self.strategy]

    async def start(self) -> None:
        self._running = True
        try:
            recent_candles = await self.storage.load_recent_candles(
                assets=self.settings.active_assets,
                timeframe_seconds=self.settings.timeframe_seconds,
                limit=self.candle_buffer.maxlen,
            )
        except Exception as exc:
            logger.warning("Could not preload recent candles: %s", exc)
        else:
            self.candle_buffer.extend(recent_candles)
            if recent_candles:
                logger.info("Loaded %s recent candles into engine buffer", len(recent_candles))
        if self.settings.paper_trading:
            self._pending_paper_trades = await self.storage.load_open_paper_trades()
        if not self.settings.signal_only and not self.settings.paper_trading and self.settings.trading_enabled:
            self._pending_broker_trades = await self.storage.load_open_broker_trades()

    async def stop(self) -> None:
        self._running = False
        self._connected = False
        current_task = asyncio.current_task()
        tasks_to_cancel = {task for task in self._tasks if task is not current_task}
        for task in tasks_to_cancel:
            task.cancel()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        self._tasks -= tasks_to_cancel

    def status(self) -> EngineStatus:
        return EngineStatus(
            running=self._running,
            connected=self._connected,
            mode=self.settings.safe_mode_name,
            open_trades=len(self._pending_paper_trades) + len(self._pending_broker_trades),
        )

    def current_client(self) -> MarketDataClient | None:
        if self._client is None or not self._client.is_connected():
            return None
        return self._client

    def set_strategy(self, strategy: BaseStrategy) -> None:
        self.strategy = strategy
        self.strategies = [strategy]

    def set_strategies(self, strategies: list[BaseStrategy]) -> None:
        if not strategies:
            raise ValueError("At least one strategy is required")
        self.strategies = strategies
        self.strategy = strategies[0]

    def set_assets(self, assets: list[str]) -> None:
        self.settings.active_assets = assets
        if not self._running or self._client is None:
            return
        watcher_tasks = {task for task in self._tasks if task.get_name() == "signal-only:ssid-watch"}
        for task in self._tasks - watcher_tasks:
            task.cancel()
        now = time.monotonic()
        self._last_candle_received_at = {asset: now for asset in self.settings.active_assets}
        self._tasks = watcher_tasks | {
            asyncio.create_task(self._consume_asset(self._client, asset), name=f"signal-only:{asset}")
            for asset in self.settings.active_assets
        }

    async def run_signal_only(self, client: MarketDataClient) -> None:
        await self.start()
        try:
            await client.connect()
            self._client = client
            self._connected = client.is_connected()
            if self.notify_pocket_connected:
                await self.notifier.pocket_connected()
            self.set_assets(self.settings.active_assets)
            if self.ssid_fingerprint_loader is not None:
                self._tasks.add(asyncio.create_task(self._watch_ssid_change(), name="signal-only:ssid-watch"))
            while self._running:
                await self._refresh_runtime_settings()
                done_tasks = {task for task in self._tasks if task.done()}
                for task in done_tasks:
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if exc is not None:
                        raise exc
                self._tasks -= done_tasks
                market_data_tasks = {task for task in self._tasks if task.get_name() != "signal-only:ssid-watch"}
                if not market_data_tasks:
                    if self._running:
                        raise RuntimeError("All market data subscriptions stopped")
                    break
                self._raise_if_market_data_stale()
                await asyncio.sleep(1)
        finally:
            self._client = None
            await client.disconnect()
            await self.stop()

    def _raise_if_market_data_stale(self) -> None:
        timeout_seconds = _market_data_timeout_seconds(self.settings.timeframe_seconds)
        now = time.monotonic()
        stale_assets = [
            asset
            for asset in self.settings.active_assets
            if now - self._last_candle_received_at.get(asset, now) > timeout_seconds
        ]
        if stale_assets:
            raise RuntimeError(f"Market data candle stream stale for {', '.join(stale_assets)}")

    async def _consume_asset(self, client: MarketDataClient, asset: str) -> None:
        subscription_result = client.subscribe_candles(asset, self.settings.timeframe_seconds)
        subscription = await subscription_result if inspect.isawaitable(subscription_result) else subscription_result
        timeout_seconds = _market_data_timeout_seconds(self.settings.timeframe_seconds)
        while self._running:
            try:
                payload = await asyncio.wait_for(subscription.__anext__(), timeout=timeout_seconds)
            except StopAsyncIteration:
                break
            except TimeoutError as exc:
                raise RuntimeError(f"Market data subscription stale for {asset}") from exc
            if not self._running:
                break
            if not isinstance(payload, dict):
                logger.debug("Skipping non-dict candle payload for %s", asset)
                continue
            await self.handle_candle_payload(asset, self.settings.timeframe_seconds, payload)
        if self._running:
            logger.warning("Market data subscription ended for %s", asset)

    async def _watch_ssid_change(self) -> None:
        if self.ssid_fingerprint_loader is None:
            return
        initial_fingerprint = await self.ssid_fingerprint_loader()
        while self._running:
            await asyncio.sleep(self.ssid_refresh_seconds)
            current_fingerprint = await self.ssid_fingerprint_loader()
            if current_fingerprint != initial_fingerprint:
                logger.info("PocketOption SSID changed; reconnecting engine")
                await self.stop()
                return

    async def handle_candle_payload(
        self,
        asset: str,
        timeframe_seconds: int,
        payload: dict,
    ) -> StrategySignal | None:
        await self._refresh_runtime_settings(force=True)
        if asset not in self.settings.active_assets:
            logger.debug("Skipping candle for inactive asset %s", asset)
            return None
        candle = parse_closed_candle(payload, asset=asset, timeframe_seconds=timeframe_seconds)
        if candle is None:
            return None

        await self.storage.save_candle(candle)
        self._last_candle_received_at[asset] = time.monotonic()
        self.candle_buffer.add(candle)
        await self._settle_due_paper_trades(candle)
        await self._settle_due_broker_trades(candle)

        candles = self.candle_buffer.latest(asset, timeframe_seconds)
        signals: list[StrategySignal] = []
        for strategy in self.strategies or [self.strategy]:
            settings = StrategySettings(
                expiration_seconds=resolve_signal_expiration(self.settings, strategy_name=strategy.name),
                timeframe_seconds=timeframe_seconds,
            )
            signal = strategy.generate_signal(asset=asset, candles=candles, settings=settings)
            if signal is not None:
                signals.append(signal)
        if not signals:
            return None

        signals = await self._apply_confidence_scores(signals, candle)
        saved_signals: list[tuple[StrategySignal, int | None]] = []
        for signal in signals:
            signal_id = await self.storage.save_signal(signal, candle, self.settings)
            saved_signals.append((signal, signal_id))

        await self._notify_signal_only_signals(signals, candle)
        await self._maybe_open_paper_trades(saved_signals, candle)
        await self._maybe_open_broker_trades(saved_signals, candle)
        return signals[0]

    async def _apply_confidence_scores(self, signals: list[StrategySignal], candle: CandleData) -> list[StrategySignal]:
        if self.confidence_scorer is None:
            return signals
        scored: list[StrategySignal] = []
        for signal in signals:
            score = await self.confidence_scorer.score(signal, now=candle.timestamp)
            reason = dict(signal.reason)
            reason["confidence"] = {
                "source": score.source,
                "sample_size": score.sample_size,
                "win_rate": str(score.win_rate) if score.win_rate is not None else None,
                "base_confidence": score.base_confidence,
            }
            scored.append(signal.model_copy(update={"confidence": score.value, "reason": reason}))
        return scored

    async def _notify_signal_only_signals(self, signals: list[StrategySignal], candle: CandleData) -> None:
        if not self.settings.signal_only or not signals:
            return
        signal = max(signals, key=_signal_notification_score)
        last_notified_at = self._last_signal_only_notification_at.get(signal.asset)
        if (
            last_notified_at is not None
            and (candle.timestamp - last_notified_at).total_seconds() < SIGNAL_ONLY_SIGNAL_NOTIFICATION_COOLDOWN_SECONDS
        ):
            return
        self._last_signal_only_notification_at[signal.asset] = candle.timestamp
        await self.notifier.new_signal(signal)

    async def _maybe_open_paper_trade(self, signal: StrategySignal, candle: CandleData, signal_id: int | None) -> None:
        await self._maybe_open_paper_trades([(signal, signal_id)], candle)

    async def _maybe_open_paper_trades(
        self,
        saved_signals: list[tuple[StrategySignal, int | None]],
        candle: CandleData,
    ) -> None:
        if not self.settings.paper_trading or self.settings.signal_only:
            return

        async with self._trade_open_lock:
            selector = self.signal_selector or SignalSelector(self.settings)
            signal_ids = _signal_ids_by_key(saved_signals)
            selection = await selector.select([signal for signal, _ in saved_signals])
            for candidate, reason in selection.rejected:
                await self.notifier.signal_rejected(candidate.signal, reason)
            if not selection.selected:
                return

            payout = self.settings.min_payout
            risk = self.risk_manager or RiskManager(self.settings)
            for candidate in selection.selected:
                signal = candidate.signal
                signal_id = signal_ids.get(_signal_key(signal))
                if reason := self._paper_open_limit_rejection(signal.asset):
                    await self.notifier.signal_rejected(signal, reason)
                    continue
                await self.notifier.signal_selected(signal)
                decision = await risk.evaluate(
                    RiskContext(
                        signal=signal,
                        signal_id=signal_id,
                        amount=self.settings.trade_amount,
                        payout=payout,
                        data_healthy=True,
                        now=candle.timestamp,
                        admin_confirmed_trading=False,
                    )
                )
                if not decision.allowed:
                    await self.notifier.signal_rejected(signal, decision.reason or "risk_rejected")
                    continue

                open_trade = self.paper_trader.open(
                    signal=signal,
                    amount=self.settings.trade_amount,
                    open_price=candle.close,
                    payout=payout,
                    opened_at=candle.timestamp,
                )
                trade_id = await self.storage.open_paper_trade(open_trade, signal_id)
                await self.notifier.paper_trade_opened(open_trade)
                self._pending_paper_trades.append(
                    PendingPaperTrade(
                        trade_id=trade_id,
                        signal=signal,
                        signal_id=signal_id,
                        amount=self.settings.trade_amount,
                        open_price=candle.close,
                        payout=payout,
                        opened_at=candle.timestamp,
                    )
                )

    async def _settle_due_paper_trades(self, candle: CandleData) -> None:
        remaining: list[PendingPaperTrade] = []
        for pending in self._pending_paper_trades:
            expires_at = pending.opened_at + timedelta(seconds=pending.signal.expiration_seconds)
            if pending.signal.asset != candle.asset or candle.timestamp < expires_at:
                remaining.append(pending)
                continue

            trade = self.paper_trader.settle(
                signal=pending.signal,
                amount=pending.amount,
                open_price=pending.open_price,
                close_price=candle.close,
                payout=pending.payout,
                opened_at=pending.opened_at,
                closed_at=candle.timestamp,
            )
            await self.storage.close_paper_trade(pending.trade_id, trade, pending.signal_id)
            await self.notifier.paper_trade_closed(trade)
        self._pending_paper_trades = remaining

    async def _maybe_open_broker_trade(self, signal: StrategySignal, candle: CandleData, signal_id: int | None) -> None:
        await self._maybe_open_broker_trades([(signal, signal_id)], candle)

    async def _maybe_open_broker_trades(
        self,
        saved_signals: list[tuple[StrategySignal, int | None]],
        candle: CandleData,
    ) -> None:
        if (
            self.settings.signal_only
            or self.settings.paper_trading
            or not self.settings.trading_enabled
            or not self._broker_trading_allowed()
        ):
            return
        if self._client is None:
            return

        async with self._trade_open_lock:
            selector = self.signal_selector or SignalSelector(self.settings)
            signal_ids = _signal_ids_by_key(saved_signals)
            selection = await selector.select([signal for signal, _ in saved_signals])
            for candidate, reason in selection.rejected:
                await self.notifier.signal_rejected(candidate.signal, reason)
            if not selection.selected:
                return
            risk = self.risk_manager or RiskManager(self.settings)
            for candidate in selection.selected:
                signal = candidate.signal
                signal_id = signal_ids.get(_signal_key(signal))
                if reason := self._broker_open_limit_rejection(signal.asset):
                    await self.notifier.signal_rejected(signal, reason)
                    continue
                await self.notifier.signal_selected(signal)
                payout = await self._payout(signal.asset)
                decision = await risk.evaluate(
                    RiskContext(
                        signal=signal,
                        signal_id=signal_id,
                        amount=self.settings.trade_amount,
                        payout=payout,
                        data_healthy=True,
                        now=candle.timestamp,
                        admin_confirmed_trading=self._admin_confirmed_for_broker_trading(),
                    )
                )
                if not decision.allowed:
                    await self.notifier.signal_rejected(signal, decision.reason or "risk_rejected")
                    continue

                trader = self.broker_trader or BrokerTrader(mode=self.settings.qt_account_mode)
                open_trade = await trader.open(
                    client=self._client,
                    signal=signal,
                    amount=self.settings.trade_amount,
                    payout=payout,
                    opened_at=candle.timestamp,
                )
                trade_id = await self.storage.open_broker_trade(open_trade, signal_id)
                await self.notifier.trade_opened(open_trade)
                self._pending_broker_trades.append(
                    PendingBrokerTrade(
                        trade_id=trade_id,
                        signal=signal,
                        signal_id=signal_id,
                        open_trade=open_trade,
                    )
                )

    def _paper_open_limit_rejection(self, asset: str) -> str | None:
        return _open_limit_rejection(
            asset=asset,
            open_assets=[pending.signal.asset for pending in self._pending_paper_trades],
            settings=self.settings,
        )

    def _broker_open_limit_rejection(self, asset: str) -> str | None:
        return _open_limit_rejection(
            asset=asset,
            open_assets=[pending.open_trade.asset for pending in self._pending_broker_trades],
            settings=self.settings,
        )

    async def _refresh_runtime_settings(self, force: bool = False) -> None:
        if self.runtime_settings_loader is None:
            return
        now = time.monotonic()
        if not force and now - self._last_runtime_settings_refresh < self.runtime_settings_refresh_seconds:
            return
        self._last_runtime_settings_refresh = now
        try:
            refreshed = await self.runtime_settings_loader(self.settings)
        except Exception as exc:
            logger.warning("Could not refresh runtime settings: %s", exc)
            return
        self._apply_runtime_settings(refreshed)

    def _apply_runtime_settings(self, refreshed: Settings) -> None:
        old_assets = list(self.settings.active_assets)
        old_strategy = self.settings.active_strategy
        old_timeframe = self.settings.timeframe_seconds

        self.settings = refreshed
        if isinstance(self.storage, DatabaseSignalOnlyStorage):
            self.storage.settings = refreshed

        if refreshed.active_strategy != old_strategy:
            try:
                self.set_strategies(create_strategies(refreshed.active_strategy))
            except UnknownStrategyError:
                logger.warning("Ignoring unavailable runtime strategy: %s", refreshed.active_strategy)

        if refreshed.active_assets != old_assets or refreshed.timeframe_seconds != old_timeframe:
            self.set_assets(refreshed.active_assets)

    async def _settle_due_broker_trades(self, candle: CandleData) -> None:
        if self._client is None:
            return

        remaining: list[PendingBrokerTrade] = []
        for pending in self._pending_broker_trades:
            expires_at = pending.open_trade.opened_at + timedelta(seconds=pending.open_trade.expiration_seconds)
            if pending.signal.asset != candle.asset or candle.timestamp < expires_at:
                remaining.append(pending)
                continue

            trader = self.broker_trader or BrokerTrader(mode=pending.open_trade.mode)
            try:
                trade = await trader.settle(self._client, pending.open_trade, closed_at=candle.timestamp)
            except Exception as exc:
                logger.warning("Broker trade settlement failed for %s: %r", pending.open_trade.external_trade_id, exc)
                trade = _demo_candle_fallback_trade_result(pending.open_trade, candle, exc)
                if trade is not None:
                    await self.storage.close_broker_trade(pending.trade_id, trade, pending.signal_id)
                    await self.notifier.trade_closed(trade)
                    continue
                if _is_final_broker_settlement_error(exc):
                    trade = _failed_broker_trade_result(pending.open_trade, candle.timestamp, exc)
                    await self.storage.close_broker_trade(pending.trade_id, trade, pending.signal_id)
                    await self.notifier.trade_closed(trade)
                    continue
                if _is_broker_settlement_too_overdue(pending.open_trade, candle):
                    trade = _failed_broker_trade_result(pending.open_trade, candle.timestamp, exc)
                    await self.storage.close_broker_trade(pending.trade_id, trade, pending.signal_id)
                    await self.notifier.trade_closed(trade)
                    continue
                remaining.append(pending)
                continue
            await self.storage.close_broker_trade(pending.trade_id, trade, pending.signal_id)
            await self.notifier.trade_closed(trade)
        self._pending_broker_trades = remaining

    async def _payout(self, asset: str) -> Decimal | None:
        if self._client is None:
            return None
        try:
            return await self._client.get_payout(asset)
        except Exception:
            return None

    def _broker_trading_allowed(self) -> bool:
        if self.settings.qt_account_mode == "demo":
            return True
        return (
            self.settings.qt_account_mode == "real"
            and self.settings.real_trading_unlocked
            and self.settings.real_trading_admin_confirmed
        )

    def _admin_confirmed_for_broker_trading(self) -> bool:
        if self.settings.qt_account_mode == "demo":
            return True
        return self.settings.real_trading_admin_confirmed


class SignalOnlyEngineRunner:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        notifier: SignalNotifier | None = None,
        client_factory: type[PocketClient] = PocketClient,
        max_reconnect_attempts: int | None = None,
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 60.0,
        notify_reconnect_after_attempts: int = 5,
        notify_reconnect_repeat_attempts: int = 30,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.notifier = notifier or NullSignalNotifier()
        self.client_factory = client_factory
        self.max_reconnect_attempts = max_reconnect_attempts
        self.initial_backoff_seconds = initial_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.notify_reconnect_after_attempts = max(1, notify_reconnect_after_attempts)
        self.notify_reconnect_repeat_attempts = max(1, notify_reconnect_repeat_attempts)
        self._stop_requested = False
        self._engine: TradingEngine | None = None
        self._last_status = EngineStatus(mode=settings.safe_mode_name)

    async def stop(self) -> None:
        self._stop_requested = True
        if self._engine is not None:
            await self._engine.stop()

    def status(self) -> EngineStatus:
        if self._engine is not None:
            return self._engine.status()
        return self._last_status

    def current_client(self) -> MarketDataClient | None:
        if self._engine is None:
            return None
        return self._engine.current_client()

    def set_strategy(self, strategy_name: str) -> None:
        strategies = create_strategies(strategy_name)
        self.settings.active_strategy = strategy_name
        if self._engine is not None:
            self._engine.set_strategies(strategies)

    def set_assets(self, assets: list[str]) -> None:
        self.settings.active_assets = assets
        if self._engine is not None:
            self._engine.set_assets(assets)

    async def run(self) -> None:
        await self.notifier.bot_started(self.settings.safe_mode_name)
        try:
            if not _is_supported_engine_mode(self.settings):
                await self._record_event(
                    "warning",
                    "engine_not_started",
                    "Engine requires signal-only, paper trading, or QT demo trading mode",
                )
                return

            try:
                strategies = create_strategies(self.settings.active_strategy)
            except UnknownStrategyError:
                await self._record_event(
                    "error",
                    "unknown_strategy",
                    "Configured strategy is not available",
                    payload={
                        "strategy": self.settings.active_strategy,
                        "available": available_strategy_names(),
                    },
                )
                return

            attempt = 0
            backoff = self.initial_backoff_seconds
            last_ssid_fingerprint: str | None = None
            while not self._stop_requested:
                self.settings = await self._load_runtime_settings()
                if not _is_supported_engine_mode(self.settings):
                    await self._record_event(
                        "warning",
                        "engine_not_started",
                        "Engine requires signal-only, paper trading, or QT demo trading mode",
                    )
                    return
                try:
                    strategies = create_strategies(self.settings.active_strategy)
                except UnknownStrategyError:
                    await self._record_event(
                        "error",
                        "unknown_strategy",
                        "Configured strategy is not available",
                        payload={
                            "strategy": self.settings.active_strategy,
                            "available": available_strategy_names(),
                        },
                    )
                    return
                ssid = await self._load_ssid()
                if not ssid:
                    await self._record_event(
                        "warning",
                        "pocket_option_ssid_missing",
                        "PocketOption SSID is not configured",
                    )
                    await self.notifier.pocket_connection_error("PocketOption SSID is not configured")
                    return
                ssid_fingerprint = _ssid_fingerprint(ssid)
                if ssid_fingerprint != last_ssid_fingerprint:
                    logger.info("Using PocketOption SSID %s", ssid_fingerprint)
                    last_ssid_fingerprint = ssid_fingerprint
                    attempt = 0
                    backoff = self.initial_backoff_seconds
                attempt += 1
                try:
                    await self._record_event("info", "signal_engine_starting", "Trading engine starting")
                    storage = DatabaseSignalOnlyStorage(self.session_factory, self.settings)
                    engine = TradingEngine(
                        settings=self.settings,
                        storage=storage,
                        strategy=strategies[0],
                        strategies=strategies,
                        notifier=self.notifier,
                        confidence_scorer=HistoricalConfidenceScorer(self.session_factory),
                        runtime_settings_loader=RuntimeSettingsService(self.session_factory).load,
                        ssid_fingerprint_loader=self._load_ssid_fingerprint,
                        notify_pocket_connected=attempt == 1 or self._should_notify_reconnect_error(attempt - 1),
                    )
                    self._engine = engine
                    await engine.run_signal_only(self.client_factory(ssid=ssid))
                    self._last_status = engine.status()
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = _sanitize_error(exc, ssid)
                    logger.warning("Trading engine error: %s", error)
                    if _should_warn_about_probable_expired_ssid(error, attempt):
                        await self.notifier.pocket_connection_error(_probable_expired_ssid_message(error))
                        await self._record_event(
                            "warning",
                            "pocket_option_ssid_probably_expired",
                            "PocketOption SSID may be expired or rejected",
                            payload={"error": error, "attempt": attempt},
                        )
                    await self._record_event(
                        "error",
                        "signal_engine_error",
                        "Trading engine error",
                        payload={"error": error, "attempt": attempt},
                    )
                    if self.max_reconnect_attempts is not None and attempt >= self.max_reconnect_attempts:
                        return
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self.max_backoff_seconds)
                finally:
                    if self._engine is not None:
                        self._last_status = self._engine.status()
                        self._engine = None
        finally:
            await self.notifier.bot_stopped()

    def _should_notify_reconnect_error(self, attempt: int) -> bool:
        if attempt < self.notify_reconnect_after_attempts:
            return False
        return (attempt - self.notify_reconnect_after_attempts) % self.notify_reconnect_repeat_attempts == 0

    async def _load_ssid(self) -> str | None:
        async with self.session_factory() as session:
            return await SecretSettingsRepository(session).get_pocket_option_ssid()

    async def _load_ssid_fingerprint(self) -> str | None:
        ssid = await self._load_ssid()
        return _ssid_fingerprint(ssid) if ssid else None

    async def _load_runtime_settings(self) -> Settings:
        try:
            return await RuntimeSettingsService(self.session_factory).load(self.settings)
        except Exception as exc:
            logger.warning("Could not load runtime settings; using current settings: %s", exc)
            return self.settings

    async def _record_event(
        self,
        level: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        async with self.session_factory() as session:
            repo = BotEventRepository(session)
            await repo.create(level=level, event_type=event_type, message=message, payload=payload)
            await session.commit()


def _sanitize_error(exc: Exception, secret: str | None) -> str:
    message = str(exc) or exc.__class__.__name__
    return message.replace(secret, "***") if secret else message


def _ssid_fingerprint(ssid: str) -> str:
    return f"len={len(ssid)} prefix={ssid[:10]!r} suffix={ssid[-10:]!r}"


def _selection_rejection_reason(rejected: list[tuple[Any, str]]) -> str:
    return rejected[0][1] if rejected else "not_selected"


def _signal_ids_by_key(saved_signals: list[tuple[StrategySignal, int | None]]) -> dict[tuple, int | None]:
    return {_signal_key(signal): signal_id for signal, signal_id in saved_signals}


def _signal_key(signal: StrategySignal) -> tuple:
    return (
        signal.asset,
        signal.strategy_name,
        signal.direction,
        signal.candle_timestamp,
        signal.expiration_seconds,
    )


def _signal_notification_score(signal: StrategySignal) -> tuple[Decimal, str]:
    confidence = Decimal(str(signal.confidence)) if signal.confidence is not None else Decimal("0")
    return confidence, signal.strategy_name


def resolve_signal_expiration(settings: Settings, strategy_name: str) -> int:
    if settings.expiration_mode == "fixed":
        return settings.expiration_seconds

    multiplier = {
        "bollinger_mean_reversion": 2,
        "trend_pullback": 3,
        "breakout_retest": 6,
    }.get(strategy_name, 3)
    return settings.timeframe_seconds * multiplier


def _open_limit_rejection(asset: str, open_assets: list[str], settings: Settings) -> str | None:
    open_trades_total = len(open_assets)
    open_trades_for_asset = sum(1 for open_asset in open_assets if open_asset == asset)
    if open_trades_total >= settings.max_open_trades_total:
        return "max_open_trades_total"
    if open_trades_for_asset >= settings.max_open_trades_per_asset:
        return "max_open_trades_per_asset"
    if settings.one_trade_per_asset and open_trades_for_asset > 0:
        return "one_trade_per_asset"
    return None


def _signal_from_trade(trade) -> StrategySignal | None:
    if trade.signal is not None:
        return StrategySignal(
            asset=trade.signal.asset,
            strategy_name=trade.signal.strategy_name,
            direction=trade.signal.direction,
            confidence=float(trade.signal.confidence) if trade.signal.confidence is not None else None,
            reason=trade.signal.reason,
            candle_timestamp=trade.signal.candle_timestamp,
            expiration_seconds=trade.signal.expiration_seconds,
        )
    if trade.opened_at is None:
        return None
    return StrategySignal(
        asset=trade.asset,
        strategy_name=trade.strategy_name,
        direction=trade.direction,
        confidence=None,
        reason={"source": "trade_restore_without_signal"},
        candle_timestamp=trade.opened_at,
        expiration_seconds=trade.expiration_seconds,
    )


def _is_final_broker_settlement_error(exc: Exception) -> bool:
    message = str(exc)
    return "Failed to find deal" in message


def _is_broker_settlement_too_overdue(open_trade: OpenBrokerTrade, candle: CandleData) -> bool:
    expires_at = open_trade.opened_at + timedelta(seconds=open_trade.expiration_seconds)
    overdue_seconds = (candle.timestamp - expires_at).total_seconds()
    return overdue_seconds >= BROKER_SETTLEMENT_MAX_OVERDUE_SECONDS


def _failed_broker_trade_result(open_trade: OpenBrokerTrade, closed_at: Any, exc: Exception) -> BrokerTradeResult:
    return BrokerTradeResult(
        mode=open_trade.mode,
        external_trade_id=open_trade.external_trade_id,
        asset=open_trade.asset,
        strategy_name=open_trade.strategy_name,
        direction=open_trade.direction,
        amount=open_trade.amount,
        expiration_seconds=open_trade.expiration_seconds,
        open_price=open_trade.open_price,
        close_price=None,
        result="unknown",
        profit=None,
        payout=open_trade.payout,
        status="failed",
        opened_at=open_trade.opened_at,
        closed_at=closed_at,
        raw_response={"error": str(exc), "source": "broker_settlement"},
    )


def _demo_candle_fallback_trade_result(
    open_trade: OpenBrokerTrade,
    candle: CandleData,
    exc: Exception,
) -> BrokerTradeResult | None:
    if open_trade.mode != "demo" or open_trade.open_price is None:
        return None

    result = _trade_result_from_prices(open_trade.direction, open_trade.open_price, candle.close)
    return BrokerTradeResult(
        mode=open_trade.mode,
        external_trade_id=open_trade.external_trade_id,
        asset=open_trade.asset,
        strategy_name=open_trade.strategy_name,
        direction=open_trade.direction,
        amount=open_trade.amount,
        expiration_seconds=open_trade.expiration_seconds,
        open_price=open_trade.open_price,
        close_price=candle.close,
        result=result,
        profit=_demo_candle_fallback_profit(result, open_trade),
        payout=open_trade.payout,
        status="closed",
        opened_at=open_trade.opened_at,
        closed_at=candle.timestamp,
        raw_response={
            "error": str(exc),
            "source": "candle_fallback",
            "broker_raw_response": open_trade.raw_response,
        },
    )


def _trade_result_from_prices(direction: str, open_price: Decimal, close_price: Decimal) -> str:
    if close_price == open_price:
        return "draw"
    if direction == "buy":
        return "win" if close_price > open_price else "loss"
    return "win" if close_price < open_price else "loss"


def _demo_candle_fallback_profit(result: str, open_trade: OpenBrokerTrade) -> Decimal | None:
    if result == "loss":
        return -open_trade.amount
    if result == "draw":
        return Decimal("0")
    if open_trade.payout is not None:
        return open_trade.amount * open_trade.payout
    raw_profit = open_trade.raw_response.get("profit")
    if raw_profit is None:
        return None
    try:
        return Decimal(str(raw_profit))
    except Exception:
        return None


def _is_supported_engine_mode(settings: Settings) -> bool:
    if settings.signal_only:
        return True
    if settings.paper_trading:
        return True
    if not settings.trading_enabled:
        return False
    if settings.qt_account_mode == "demo":
        return True
    return settings.qt_account_mode == "real" and settings.real_trading_unlocked and settings.real_trading_admin_confirmed
