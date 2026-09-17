from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analytics.stats import AnalyticsService, format_stats_summary
from app.config import get_settings
from app.db.session import create_session_factory
from app.logging_config import setup_logging
from app.runtime_settings import RuntimeSettingsService
from app.telegram.bot import run_bot
from app.telegram.notifications import DatabaseTradeMessageStore, TelegramNotifier
from app.telegram.session import create_telegram_bot
from app.trading.engine import EngineStatus, SignalOnlyEngineRunner
from app.trading.pocket_service import PocketConnectionService

logger = logging.getLogger(__name__)
ENGINE_STATUS_PATH = Path(os.getenv("POCKET_ENGINE_STATUS_PATH", "/tmp/pocket_option_bot_engine_status.json"))
ENGINE_STATUS_MAX_AGE_SECONDS = 15.0


async def main() -> None:
    settings = get_settings()
    session_factory = create_session_factory(settings)
    try:
        settings = await RuntimeSettingsService(session_factory).load(settings)
    except Exception as exc:
        logger.warning("Runtime settings could not be loaded; using environment settings: %s", exc)

    setup_logging(settings)
    logger.info(
        "Starting app env=%s signal_only=%s trading_enabled=%s assets=%s worker=%s",
        settings.app_env,
        settings.signal_only,
        settings.trading_enabled,
        ",".join(settings.active_assets),
        _is_engine_worker(),
    )
    if _is_engine_worker():
        await _run_engine_worker(settings)
        return

    engine_process = await _start_engine_process()
    bot_status_provider = ExternalEngineStatusProvider(settings)
    try:
        await run_bot(
            settings,
            session_factory=session_factory,
            engine_status_provider=bot_status_provider,
        )
    finally:
        await _stop_engine_process(engine_process)


async def _run_engine_worker(settings) -> None:
    session_factory = create_session_factory(settings)
    engine_bot = create_telegram_bot(settings.telegram_bot_token) if settings.telegram_bot_token else None
    notifier = (
        TelegramNotifier(
            engine_bot,
            settings,
            min_send_interval_seconds=1.2,
            trade_message_store=DatabaseTradeMessageStore(session_factory),
        )
        if engine_bot
        else None
    )
    engine_runner = SignalOnlyEngineRunner(settings=settings, session_factory=session_factory, notifier=notifier)
    balance_service = PocketConnectionService(
        session_factory,
        connected_client_provider=engine_runner,
        account_type=settings.qt_account_mode,
    )
    status_task = asyncio.create_task(_publish_engine_status(engine_runner, ENGINE_STATUS_PATH))
    hourly_stats_task = (
        asyncio.create_task(
            _send_hourly_stats(notifier, session_factory, balance_service, settings.qt_account_mode),
            name="hourly-stats",
        )
        if notifier
        else None
    )
    try:
        await engine_runner.run()
    finally:
        status_task.cancel()
        tasks = [status_task]
        if hourly_stats_task is not None:
            hourly_stats_task.cancel()
            tasks.append(hourly_stats_task)
        await asyncio.gather(*tasks, return_exceptions=True)
        _write_engine_status(ENGINE_STATUS_PATH, engine_runner.status())


async def _start_engine_process() -> asyncio.subprocess.Process:
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.main", "--engine-worker")
    logger.info("Started engine worker process pid=%s", process.pid)
    return process


async def _stop_engine_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    logger.info("Stopping engine worker process pid=%s", process.pid)
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        process.kill()
        await process.wait()


def _is_engine_worker() -> bool:
    return "--engine-worker" in sys.argv


class ExternalEngineStatusProvider:
    def __init__(self, settings, status_path: Path = ENGINE_STATUS_PATH) -> None:
        self.settings = settings
        self.status_path = status_path

    def status(self) -> EngineStatus:
        status = _read_engine_status(self.status_path)
        return status if status is not None else EngineStatus(mode=self.settings.safe_mode_name)

    def set_strategy(self, strategy_name: str) -> None:
        self.settings.active_strategy = strategy_name

    def set_assets(self, assets: list[str]) -> None:
        self.settings.active_assets = assets


async def _publish_engine_status(engine_runner: SignalOnlyEngineRunner, path: Path) -> None:
    while True:
        _write_engine_status(path, engine_runner.status())
        await asyncio.sleep(1)


async def _send_hourly_stats(
    notifier: TelegramNotifier,
    session_factory: async_sessionmaker[AsyncSession],
    balance_service: PocketConnectionService | None = None,
    account_type: str | None = None,
) -> None:
    while True:
        await asyncio.sleep(_seconds_until_next_hour(datetime.now(UTC)))
        await _send_stats_1h_once(notifier, session_factory, balance_service=balance_service, account_type=account_type)


async def _send_stats_1h_once(
    notifier: TelegramNotifier,
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
    balance_service: PocketConnectionService | None = None,
    account_type: str | None = None,
) -> None:
    if balance_service is not None:
        await balance_service.get_balance(source="hourly_stats")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start_at = now - timedelta(hours=1)
    try:
        stats = await AnalyticsService(session_factory).stats_since(start_at, end_at=now, account_type=account_type)
    except Exception as exc:
        logger.warning("Hourly stats could not be loaded: %s", exc)
        return
    await notifier.send(format_stats_summary(stats, title="Stats last 1h"))


def _seconds_until_next_hour(now: datetime) -> float:
    now = now.astimezone(UTC)
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    return max((next_hour - now).total_seconds(), 0.0)


def _write_engine_status(path: Path, status: EngineStatus) -> None:
    path.write_text(
        json.dumps(
            {
                "running": status.running,
                "connected": status.connected,
                "mode": status.mode,
                "open_trades": status.open_trades,
                "updated_at": time.time(),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _read_engine_status(path: Path) -> EngineStatus | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    updated_at = payload.get("updated_at")
    if not isinstance(updated_at, int | float) or time.time() - updated_at > ENGINE_STATUS_MAX_AGE_SECONDS:
        return None
    return EngineStatus(
        running=bool(payload.get("running")),
        connected=bool(payload.get("connected")),
        mode=str(payload.get("mode") or "signal_only"),
        open_trades=int(payload.get("open_trades") or 0),
    )


if __name__ == "__main__":
    asyncio.run(main())
