from datetime import UTC, datetime
from decimal import Decimal

from app.analytics.stats import (
    AssetBreakdown,
    ModeBreakdown,
    StrategyBreakdown,
    TodayStats,
    TradeSummary,
    format_last_trades,
    format_open_trades,
    format_paper_report,
    format_paper_status,
    format_stats_summary,
    format_today_stats,
    parse_stats_period,
)


def test_format_today_stats_empty() -> None:
    assert format_today_stats(TodayStats()) == (
        "Today stats\n"
        "Signals: 0\n"
        "Selected signals: 0\n"
        "Trades: 0\n"
        "Wins/Losses/Draws: 0/0/0\n"
        "Win rate: 0.00%\n"
        "PnL: 0.00\n"
        "Best asset: unknown\n"
        "Worst asset: unknown"
    )


def test_format_today_stats_with_strategy_breakdown() -> None:
    text = format_today_stats(
        TodayStats(
            signals_count=12,
            selected_signals_count=3,
            trades_count=3,
            wins=2,
            losses=1,
            pnl=Decimal("1.25"),
            best_asset="EURUSD_otc",
            worst_asset="GBPUSD_otc",
            strategy_breakdown=[
                StrategyBreakdown(
                    strategy_name="trend_pullback",
                    trades_count=3,
                    wins=2,
                    losses=1,
                    draws=0,
                    pnl=Decimal("1.25"),
                )
            ],
        )
    )

    assert "Win rate: 66.67%" in text
    assert "- trend_pullback: trades=3, W/L/D=2/1/0, PnL=1.25" in text


def test_format_today_stats_with_mode_breakdown() -> None:
    text = format_today_stats(
        TodayStats(
            mode_breakdown=[
                ModeBreakdown(mode="demo", trades_count=1, wins=0, losses=1, draws=0, pnl=Decimal("-1")),
                ModeBreakdown(mode="paper", trades_count=2, wins=1, losses=0, draws=1, pnl=Decimal("0.8")),
            ]
        )
    )

    assert "Modes:" in text
    assert "- demo: trades=1, W/L/D=0/1/0, win_rate=0.00%, PnL=-1.00" in text
    assert "- paper: trades=2, W/L/D=1/0/1, win_rate=50.00%, PnL=0.80" in text


def test_format_stats_with_custom_title_and_asset_breakdown() -> None:
    text = format_today_stats(
        TodayStats(
            trades_count=3,
            wins=2,
            losses=1,
            pnl=Decimal("1.2"),
            balance_start=Decimal("1000"),
            balance_end=Decimal("1001.2"),
            best_asset="EURUSD_otc",
            worst_asset="GBPUSD_otc",
            asset_breakdown=[
                AssetBreakdown(asset="EURUSD_otc", trades_count=2, wins=2, losses=0, draws=0, pnl=Decimal("1.6")),
                AssetBreakdown(asset="GBPUSD_otc", trades_count=1, wins=0, losses=1, draws=0, pnl=Decimal("-0.4")),
            ],
        ),
        title="Stats last 6h",
    )

    assert text.startswith("Stats last 6h\n")
    assert "Assets:" in text
    assert "- EURUSD_otc: trades=2, W/L/D=2/0/0, win_rate=100.00%, PnL=1.60" in text
    assert "- GBPUSD_otc: trades=1, W/L/D=0/1/0, win_rate=0.00%, PnL=-0.40" in text


def test_parse_stats_period_hours_and_days() -> None:
    now = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)

    hour_period = parse_stats_period("12h", now=now)
    day_period = parse_stats_period("2d", now=now)

    assert hour_period is not None
    assert hour_period.label == "last 12h"
    assert hour_period.start_at == datetime(2026, 5, 10, 0, 0, tzinfo=UTC)
    assert day_period is not None
    assert day_period.label == "last 2d"
    assert day_period.start_at == datetime(2026, 5, 8, 12, 0, tzinfo=UTC)


def test_parse_stats_period_rejects_invalid_values() -> None:
    assert parse_stats_period("") is None
    assert parse_stats_period("0h") is None
    assert parse_stats_period("12m") is None
    assert parse_stats_period("abc") is None


def test_format_stats_summary() -> None:
    text = format_stats_summary(
        TodayStats(
            signals_count=120,
            selected_signals_count=5,
            trades_count=3,
            wins=2,
            losses=1,
            pnl=Decimal("1.2"),
            balance_start=Decimal("1000"),
            balance_end=Decimal("1001.2"),
            best_asset="EURUSD_otc",
            worst_asset="GBPUSD_otc",
            asset_breakdown=[
                AssetBreakdown(asset="EURUSD_otc", trades_count=2, wins=2, losses=0, draws=0, pnl=Decimal("2")),
                AssetBreakdown(asset="GBPUSD_otc", trades_count=1, wins=0, losses=1, draws=0, pnl=Decimal("-0.8")),
            ],
            strategy_breakdown=[
                StrategyBreakdown(
                    strategy_name="bollinger_mean_reversion",
                    trades_count=3,
                    wins=2,
                    losses=1,
                    draws=0,
                    pnl=Decimal("1.2"),
                )
            ],
        ),
        title="Stats last 1h",
    )

    assert "📈 Stats last 1h" in text
    assert "💰 PnL: +1.20" in text
    assert "🏦 Balance: 1000.00 -> 1001.20 (+1.20)" in text
    assert "📌 Assets" in text
    assert "💸 GBPUSD_otc: 1 trades, 0/1/0, WR 0.00%, PnL -0.80" in text
    assert "🧠 Strategies" in text


def test_format_last_trades_empty() -> None:
    assert format_last_trades([]) == "Last trades\nNo trades yet."


def test_format_last_trades() -> None:
    text = format_last_trades(
        [
            TradeSummary(
                mode="paper",
                asset="EURUSD_otc",
                strategy_name="trend_pullback",
                direction="buy",
                amount=Decimal("1"),
                result="win",
                profit=Decimal("0.8"),
                status="closed",
                opened_at=datetime(2026, 1, 1, 12, 30, tzinfo=UTC),
            )
        ]
    )

    assert text == (
        "Last trades\n"
        "2026-01-01 12:30 UTC | EURUSD_otc | buy | closed/win | mode=paper | amount=1.00 | profit=0.80"
    )


def test_format_open_trades_empty() -> None:
    assert format_open_trades([]) == "Open trades\nNo open trades."


def test_format_open_trades() -> None:
    text = format_open_trades(
        [
            TradeSummary(
                id=7,
                mode="paper",
                asset="EURUSD_otc",
                strategy_name="trend_pullback",
                direction="buy",
                amount=Decimal("1"),
                result="unknown",
                profit=None,
                status="opened",
                opened_at=datetime(2026, 1, 1, 12, 30, tzinfo=UTC),
            )
        ]
    )

    assert text == (
        "Open trades\n"
        "#7 | 2026-01-01 12:30 UTC | EURUSD_otc | buy | strategy=trend_pullback | mode=paper | amount=1.00"
    )


def test_format_paper_status() -> None:
    text = format_paper_status(
        paper_enabled=True,
        open_trades=[
            TradeSummary(
                id=7,
                mode="paper",
                asset="EURUSD_otc",
                strategy_name="trend_pullback",
                direction="buy",
                amount=Decimal("1"),
                result="unknown",
                profit=None,
                status="opened",
                opened_at=datetime(2026, 1, 1, 12, 30, tzinfo=UTC),
            )
        ],
        stats=TodayStats(trades_count=2, wins=1, losses=1, pnl=Decimal("-0.2")),
        assets=["EURUSD_otc"],
        strategy="trend_pullback",
    )

    assert text == (
        "Paper status\n"
        "Enabled: True\n"
        "Open paper trades: 1\n"
        "Today trades: 2\n"
        "Wins/Losses/Draws: 1/1/0\n"
        "Win rate: 50.00%\n"
        "PnL: -0.20\n"
        "Strategy: trend_pullback\n"
        "Assets: EURUSD_otc"
    )


def test_format_paper_report() -> None:
    text = format_paper_report(
        TodayStats(
            signals_count=3,
            selected_signals_count=2,
            trades_count=2,
            wins=1,
            losses=1,
            pnl=Decimal("-0.2"),
            best_asset="EURUSD_otc",
            worst_asset="GBPUSD_otc",
        )
    )

    assert text == (
        "Paper report\n"
        "Signals: 3\n"
        "Selected signals: 2\n"
        "Trades: 2\n"
        "Wins/Losses/Draws: 1/1/0\n"
        "Win rate: 50.00%\n"
        "PnL: -0.20\n"
        "Best asset: EURUSD_otc\n"
        "Worst asset: GBPUSD_otc"
    )
