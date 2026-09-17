from app.db.models import Base


def test_expected_tables_are_declared() -> None:
    assert {
        "telegram_users",
        "settings",
        "candles",
        "signals",
        "trades",
        "bot_events",
        "balance_snapshots",
        "strategy_metrics",
    }.issubset(Base.metadata.tables)


def test_trades_have_mode_column() -> None:
    assert "mode" in Base.metadata.tables["trades"].columns
