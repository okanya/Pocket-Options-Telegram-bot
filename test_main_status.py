import json
import time
from datetime import UTC, datetime

from app.main import ExternalEngineStatusProvider, _read_engine_status, _seconds_until_next_hour, _write_engine_status
from app.trading.engine import EngineStatus


class SettingsStub:
    safe_mode_name = "signal_only"
    active_strategy = "all"
    active_assets = ["EURUSD_otc"]


def test_engine_status_snapshot_round_trip(tmp_path) -> None:
    path = tmp_path / "engine-status.json"

    _write_engine_status(path, EngineStatus(running=True, connected=True, mode="demo", open_trades=2))

    assert _read_engine_status(path) == EngineStatus(running=True, connected=True, mode="demo", open_trades=2)


def test_engine_status_snapshot_ignores_stale_file(tmp_path) -> None:
    path = tmp_path / "engine-status.json"
    path.write_text(
        json.dumps(
            {
                "running": True,
                "connected": True,
                "mode": "demo",
                "open_trades": 1,
                "updated_at": time.time() - 60,
            }
        ),
        encoding="utf-8",
    )

    assert _read_engine_status(path) is None


def test_external_status_provider_uses_snapshot(tmp_path) -> None:
    path = tmp_path / "engine-status.json"
    _write_engine_status(path, EngineStatus(running=True, connected=True, mode="paper", open_trades=1))

    provider = ExternalEngineStatusProvider(SettingsStub(), status_path=path)

    assert provider.status() == EngineStatus(running=True, connected=True, mode="paper", open_trades=1)


def test_external_status_provider_falls_back_to_settings(tmp_path) -> None:
    provider = ExternalEngineStatusProvider(SettingsStub(), status_path=tmp_path / "missing.json")

    assert provider.status() == EngineStatus(mode="signal_only")


def test_seconds_until_next_hour() -> None:
    assert _seconds_until_next_hour(datetime(2026, 5, 11, 10, 15, 30, tzinfo=UTC)) == 2670
    assert _seconds_until_next_hour(datetime(2026, 5, 11, 10, 0, 0, tzinfo=UTC)) == 3600
