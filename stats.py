from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import re

from sqlalchemy import case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BalanceSnapshot, Signal, Trade


@dataclass(frozen=True)
class StrategyBreakdown:
    strategy_name: str
    trades_count: int
    wins: int
    losses: int
    draws: int
    pnl: Decimal

    @property
    def win_rate(self) -> Decimal:
        return _win_rate(self.wins, self.losses, self.draws)


@dataclass(frozen=True)
class ModeBreakdown:
    mode: str
    trades_count: int
    wins: int
    losses: int
    draws: int
    pnl: Decimal

    @property
    def win_rate(self) -> Decimal:
        return _win_rate(self.wins, self.losses, self.draws)


@dataclass(frozen=True)
class AssetBreakdown:
    asset: str
    trades_count: int
    wins: int
    losses: int
    draws: int
    pnl: Decimal

    @property
    def win_rate(self) -> Decimal:
        return _win_rate(self.wins, self.losses, self.draws)


@dataclass(frozen=True)
class StatsPeriod:
    label: str
    start_at: datetime


@dataclass(frozen=True)
class TodayStats:
    signals_count: int = 0
    selected_signals_count: int = 0
    trades_count: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    pnl: Decimal = Decimal("0")
    balance_start: Decimal | None = None
    balance_end: Decimal | None = None
    best_asset: str | None = None
    worst_asset: str | None = None
    asset_breakdown: list[AssetBreakdown] = field(default_factory=list)
    mode_breakdown: list[ModeBreakdown] = field(default_factory=list)
    strategy_breakdown: list[StrategyBreakdown] = field(default_factory=list)

    @property
    def win_rate(self) -> Decimal:
        return _win_rate(self.wins, self.losses, self.draws)


@dataclass(frozen=True)
class TradeSummary:
    mode: str
    asset: str
    strategy_name: str
    direction: str
    amount: Decimal
    result: str
    profit: Decimal | None
    status: str
    opened_at: datetime
    id: int | None = None


class AnalyticsService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def today_stats(
        self,
        now: datetime | None = None,
        mode: str | None = None,
        account_type: str | None = None,
    ) -> TodayStats:
        now = now or datetime.now(UTC)
        day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return await self.stats_since(day_start, mode=mode, end_at=now, account_type=account_type)

    async def stats_since(
        self,
        start_at: datetime,
        mode: str | None = None,
        end_at: datetime | None = None,
        account_type: str | None = None,
    ) -> TodayStats:
        start_at = start_at.astimezone(UTC)
        end_at = (end_at or datetime.now(UTC)).astimezone(UTC)
        async with self.session_factory() as session:
            signal_filters = [Signal.created_at >= start_at, Signal.created_at < end_at]
            trade_filters = [
                Trade.status == "closed",
                Trade.closed_at >= start_at,
                Trade.closed_at < end_at,
            ]
            if mode is not None:
                signal_filters.append(Signal.mode == mode)
                trade_filters.append(Trade.mode == mode)

            signals_count = await _scalar_int(session, select(func.count()).select_from(Signal).where(*signal_filters))
            selected_signals_count = await _scalar_int(
                session, select(func.count()).select_from(Signal).where(*signal_filters, Signal.is_selected.is_(True))
            )
            trade_row = (
                await session.execute(
                    select(
                        func.count(Trade.id),
                        _count_result("win"),
                        _count_result("loss"),
                        _count_result("draw"),
                        func.coalesce(func.sum(Trade.profit), 0),
                    ).where(*trade_filters)
                )
            ).one()
            asset_rows = (
                await session.execute(
                    select(
                        Trade.asset,
                        func.count(Trade.id),
                        _count_result("win"),
                        _count_result("loss"),
                        _count_result("draw"),
                        func.coalesce(func.sum(Trade.profit), 0).label("pnl"),
                    )
                    .where(*trade_filters)
                    .group_by(Trade.asset)
                    .order_by(desc("pnl"))
                )
            ).all()
            strategy_rows = (
                await session.execute(
                    select(
                        Trade.strategy_name,
                        func.count(Trade.id),
                        _count_result("win"),
                        _count_result("loss"),
                        _count_result("draw"),
                        func.coalesce(func.sum(Trade.profit), 0),
                    )
                    .where(*trade_filters)
                    .group_by(Trade.strategy_name)
                    .order_by(Trade.strategy_name)
                )
            ).all()
            mode_rows = (
                await session.execute(
                    select(
                        Trade.mode,
                        func.count(Trade.id),
                        _count_result("win"),
                        _count_result("loss"),
                        _count_result("draw"),
                        func.coalesce(func.sum(Trade.profit), 0),
                    )
                    .where(*trade_filters)
                    .group_by(Trade.mode)
                    .order_by(Trade.mode)
                )
            ).all()
            balance_start = await _latest_balance_at_or_before(session, start_at, account_type=account_type)
            balance_end = await _latest_balance_at_or_before(session, end_at, account_type=account_type)

        return TodayStats(
            signals_count=signals_count,
            selected_signals_count=selected_signals_count,
            trades_count=int(trade_row[0] or 0),
            wins=int(trade_row[1] or 0),
            losses=int(trade_row[2] or 0),
            draws=int(trade_row[3] or 0),
            pnl=Decimal(trade_row[4] or 0),
            balance_start=balance_start,
            balance_end=balance_end,
            best_asset=asset_rows[0][0] if asset_rows else None,
            worst_asset=asset_rows[-1][0] if asset_rows else None,
            asset_breakdown=[
                AssetBreakdown(
                    asset=row[0],
                    trades_count=int(row[1] or 0),
                    wins=int(row[2] or 0),
                    losses=int(row[3] or 0),
                    draws=int(row[4] or 0),
                    pnl=Decimal(row[5] or 0),
                )
                for row in asset_rows
            ],
            mode_breakdown=[
                ModeBreakdown(
                    mode=row[0],
                    trades_count=int(row[1] or 0),
                    wins=int(row[2] or 0),
                    losses=int(row[3] or 0),
                    draws=int(row[4] or 0),
                    pnl=Decimal(row[5] or 0),
                )
                for row in mode_rows
            ],
            strategy_breakdown=[
                StrategyBreakdown(
                    strategy_name=row[0],
                    trades_count=int(row[1] or 0),
                    wins=int(row[2] or 0),
                    losses=int(row[3] or 0),
                    draws=int(row[4] or 0),
                    pnl=Decimal(row[5] or 0),
                )
                for row in strategy_rows
            ],
        )

    async def last_trades(self, limit: int = 10) -> list[TradeSummary]:
        async with self.session_factory() as session:
            rows = await session.scalars(select(Trade).order_by(Trade.opened_at.desc()).limit(limit))
            return [
                TradeSummary(
                    id=trade.id,
                    mode=trade.mode,
                    asset=trade.asset,
                    strategy_name=trade.strategy_name,
                    direction=trade.direction,
                    amount=trade.amount,
                    result=trade.result,
                    profit=trade.profit,
                    status=trade.status,
                    opened_at=trade.opened_at,
                )
                for trade in rows
            ]

    async def open_trades(self) -> list[TradeSummary]:
        async with self.session_factory() as session:
            rows = await session.scalars(select(Trade).where(Trade.status == "opened").order_by(Trade.opened_at.desc()))
            return [
                TradeSummary(
                    id=trade.id,
                    mode=trade.mode,
                    asset=trade.asset,
                    strategy_name=trade.strategy_name,
                    direction=trade.direction,
                    amount=trade.amount,
                    result=trade.result,
                    profit=trade.profit,
                    status=trade.status,
                    opened_at=trade.opened_at,
                )
                for trade in rows
            ]


def parse_stats_period(value: str, now: datetime | None = None) -> StatsPeriod | None:
    value = value.strip().lower()
    if not value:
        return None
    match = re.fullmatch(r"([1-9]\d*)([hd])", value)
    if match is None:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if unit == "h":
        return StatsPeriod(label=f"last {amount}h", start_at=now - timedelta(hours=amount))
    return StatsPeriod(label=f"last {amount}d", start_at=now - timedelta(days=amount))


def format_today_stats(stats: TodayStats, title: str = "Today stats") -> str:
    lines = [
        title,
        f"Signals: {stats.signals_count}",
        f"Selected signals: {stats.selected_signals_count}",
        f"Trades: {stats.trades_count}",
        f"Wins/Losses/Draws: {stats.wins}/{stats.losses}/{stats.draws}",
        f"Win rate: {_format_percent(stats.win_rate)}",
        f"PnL: {_format_decimal(stats.pnl)}",
        f"Best asset: {stats.best_asset or 'unknown'}",
        f"Worst asset: {stats.worst_asset or 'unknown'}",
    ]
    if stats.balance_start is not None or stats.balance_end is not None:
        lines.append(f"Balance: {_format_balance_change(stats.balance_start, stats.balance_end)}")
    if stats.strategy_breakdown:
        lines.append("Strategies:")
        lines.extend(
            f"- {item.strategy_name}: trades={item.trades_count}, "
            f"W/L/D={item.wins}/{item.losses}/{item.draws}, PnL={_format_decimal(item.pnl)}"
            for item in stats.strategy_breakdown
        )
    if stats.asset_breakdown:
        lines.append("Assets:")
        lines.extend(
            f"- {item.asset}: trades={item.trades_count}, W/L/D={item.wins}/{item.losses}/{item.draws}, "
            f"win_rate={_format_percent(item.win_rate)}, PnL={_format_decimal(item.pnl)}"
            for item in stats.asset_breakdown
        )
    if stats.mode_breakdown:
        lines.append("Modes:")
        lines.extend(
            f"- {item.mode}: trades={item.trades_count}, W/L/D={item.wins}/{item.losses}/{item.draws}, "
            f"win_rate={_format_percent(item.win_rate)}, PnL={_format_decimal(item.pnl)}"
            for item in stats.mode_breakdown
        )
    return "\n".join(lines)


def format_stats_summary(stats: TodayStats, title: str = "Stats last 1h") -> str:
    lines = [
        f"📈 {title}",
        f"🔔 Signals: {stats.signals_count} | selected: {stats.selected_signals_count}",
        f"🎯 Trades: {stats.trades_count}",
        f"✅/🔴/⚪ W/L/D: {stats.wins}/{stats.losses}/{stats.draws}",
        f"📊 Win rate: {_format_percent(stats.win_rate)}",
        f"{_profit_icon(stats.pnl)} PnL: {_format_signed_decimal(stats.pnl)}",
        f"🏦 Balance: {_format_balance_change(stats.balance_start, stats.balance_end)}",
        f"🏆 Best: {stats.best_asset or 'unknown'}",
        f"⚠️ Worst: {stats.worst_asset or 'unknown'}",
    ]
    if stats.asset_breakdown:
        lines.append("")
        lines.append("📌 Assets")
        lines.extend(
            f"{_profit_icon(item.pnl)} {item.asset}: {item.trades_count} trades, "
            f"{item.wins}/{item.losses}/{item.draws}, WR {_format_percent(item.win_rate)}, "
            f"PnL {_format_signed_decimal(item.pnl)}"
            for item in stats.asset_breakdown[:8]
        )
    if stats.strategy_breakdown:
        lines.append("")
        lines.append("🧠 Strategies")
        lines.extend(
            f"{_profit_icon(item.pnl)} {item.strategy_name}: {item.trades_count} trades, "
            f"{item.wins}/{item.losses}/{item.draws}, PnL {_format_signed_decimal(item.pnl)}"
            for item in stats.strategy_breakdown
        )
    return "\n".join(lines)


def format_last_trades(trades: list[TradeSummary]) -> str:
    if not trades:
        return "Last trades\nNo trades yet."

    lines = ["Last trades"]
    for trade in trades:
        opened_at = trade.opened_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(
            f"{opened_at} | {trade.asset} | {trade.direction} | {trade.status}/{trade.result} | "
            f"mode={trade.mode} | amount={_format_decimal(trade.amount)} | profit={_format_decimal(trade.profit)}"
        )
    return "\n".join(lines)


def format_open_trades(trades: list[TradeSummary]) -> str:
    if not trades:
        return "Open trades\nNo open trades."

    lines = ["Open trades"]
    for trade in trades:
        opened_at = trade.opened_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        trade_id = trade.id if trade.id is not None else "unknown"
        lines.append(
            f"#{trade_id} | {opened_at} | {trade.asset} | {trade.direction} | "
            f"strategy={trade.strategy_name} | mode={trade.mode} | amount={_format_decimal(trade.amount)}"
        )
    return "\n".join(lines)


def format_paper_status(paper_enabled: bool, open_trades: list[TradeSummary], stats: TodayStats, assets: list[str], strategy: str) -> str:
    return "\n".join(
        [
            "Paper status",
            f"Enabled: {paper_enabled}",
            f"Open paper trades: {len(open_trades)}",
            f"Today trades: {stats.trades_count}",
            f"Wins/Losses/Draws: {stats.wins}/{stats.losses}/{stats.draws}",
            f"Win rate: {_format_percent(stats.win_rate)}",
            f"PnL: {_format_decimal(stats.pnl)}",
            f"Strategy: {strategy}",
            f"Assets: {', '.join(assets)}",
        ]
    )


def format_paper_report(stats: TodayStats) -> str:
    lines = [
        "Paper report",
        f"Signals: {stats.signals_count}",
        f"Selected signals: {stats.selected_signals_count}",
        f"Trades: {stats.trades_count}",
        f"Wins/Losses/Draws: {stats.wins}/{stats.losses}/{stats.draws}",
        f"Win rate: {_format_percent(stats.win_rate)}",
        f"PnL: {_format_decimal(stats.pnl)}",
        f"Best asset: {stats.best_asset or 'unknown'}",
        f"Worst asset: {stats.worst_asset or 'unknown'}",
    ]
    if stats.strategy_breakdown:
        lines.append("Strategies:")
        lines.extend(
            f"- {item.strategy_name}: trades={item.trades_count}, "
            f"W/L/D={item.wins}/{item.losses}/{item.draws}, PnL={_format_decimal(item.pnl)}"
            for item in stats.strategy_breakdown
        )
    return "\n".join(lines)


def _count_result(result: str):
    return func.coalesce(func.sum(case((Trade.result == result, 1), else_=0)), 0)


async def _latest_balance_at_or_before(
    session: AsyncSession,
    at: datetime,
    account_type: str | None = None,
) -> Decimal | None:
    stmt = (
        select(BalanceSnapshot.balance)
        .where(BalanceSnapshot.created_at <= at)
        .order_by(desc(BalanceSnapshot.created_at))
        .limit(1)
    )
    if account_type is not None:
        stmt = stmt.where(BalanceSnapshot.account_type == account_type)
    value = await session.scalar(stmt)
    return Decimal(value) if value is not None else None


async def _scalar_int(session: AsyncSession, stmt) -> int:
    return int(await session.scalar(stmt) or 0)


def _format_percent(value: Decimal) -> str:
    return f"{value * Decimal('100'):.2f}%"


def _format_decimal(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.2f}"


def _format_signed_decimal(value: Decimal) -> str:
    return f"+{value:.2f}" if value > 0 else f"{value:.2f}"


def _format_balance_change(start: Decimal | None, end: Decimal | None) -> str:
    start_text = _format_decimal(start)
    end_text = _format_decimal(end)
    if start is None or end is None:
        return f"{start_text} -> {end_text}"
    return f"{start_text} -> {end_text} ({_format_signed_decimal(end - start)})"


def _profit_icon(value: Decimal) -> str:
    if value > 0:
        return "💰"
    if value < 0:
        return "💸"
    return "➖"


def _win_rate(wins: int, losses: int, draws: int) -> Decimal:
    finished = wins + losses + draws
    if finished == 0:
        return Decimal("0")
    return Decimal(wins) / Decimal(finished)
