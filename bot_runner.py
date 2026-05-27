"""
Single-container entry point — all tasks run in one asyncio event loop:
  - WebSocket ingestion   continuous, auto-restart on failure
  - weather ingestion     hourly (Open-Meteo forecast for all 19 cities)
  - trade_cycle()         every TRADE_CYCLE_INTERVAL_SECS  (default 300s / 5 min)
  - monitor_positions()   every MONITOR_INTERVAL_SECS       (default 120s / 2 min)
  - db_prune()            once daily at PRUNE_HOUR_UTC       (default 04:00 UTC)

Historical weather bootstrap runs once at startup if the DB is empty.
The halt flag pauses trade_cycle and monitor_positions immediately.
"""
import sys
import asyncio
import logging
import os
import time
from datetime import datetime, timezone, date as _date, timedelta
from pathlib import Path
from typing import Dict, Optional

from aiohttp import web
from src.index import trade_cycle, monitor_positions
from src.ingest.weather import WeatherIngestor
from src.config.env import Config
from src.db.dwtrader import DWTraderDB
from src.db.maintenance import prune as _db_prune
from src.services.kalshi_client import client
from src.config.experiment import GUMBEL_SCHEDULE

# check_outcomes lives in scripts/ — add it to path so bot_runner can call run_check directly.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from check_outcomes import run_check as _run_outcome_check

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("BotRunner")

# Absolute path — same anchor as RiskManager so the halt flag is always found.
_PROJECT_ROOT = Path(__file__).resolve().parent
HALT_PATH     = _PROJECT_ROOT / "data" / "halt.flag"

TRADE_CYCLE_INTERVAL = int(os.getenv("TRADE_CYCLE_INTERVAL_SECS", "300"))
MONITOR_INTERVAL     = int(os.getenv("MONITOR_INTERVAL_SECS", "120"))
PRUNE_HOUR_UTC       = int(os.getenv("PRUNE_HOUR_UTC", "4"))   # 4am UTC = after all settlements
INNER_SLEEP          = 10   # main-loop polling resolution (seconds)
# GUMBEL_SCHEDULE is imported from src/config/experiment.py — edit there to add dates.


def _env_mode() -> str:
    """Return env_mode as an uppercase string (matches DB CHECK constraint)."""
    mode = getattr(Config, "ENV_EXECUTION_MODE", "PAPER")
    # Config now returns a plain str, but guard against accidental Enum usage.
    return mode.value.upper() if hasattr(mode, "value") else str(mode).upper()


def _make_ws_task(pipeline, ws_url: str) -> asyncio.Task:
    """Spawn a fresh WebSocket connect task reusing the existing pipeline/db."""
    from src.ingest.ws_client import KalshiWebSocketClient
    ws = KalshiWebSocketClient(pipeline, ws_url, client)
    task = asyncio.create_task(ws.connect(), name="websocket-ingest")
    logger.info("WebSocket ingestion task started (%s)", ws_url)
    return task


async def _health_server() -> None:
    """Minimal HTTP server so Azure Web App health probes pass."""
    port = int(os.getenv("WEBSITES_PORT", "8000"))
    app = web.Application()
    app.router.add_get("/",       lambda r: web.Response(text="OK"))
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Health server listening on port %d", port)
    await asyncio.Event().wait()   # run forever


async def _bootstrap_historical(db: DWTraderDB) -> None:
    """Seed 90 days of weather history on first run; no-op if data already exists."""
    with db.get_connection() as conn:
        n = conn.execute("SELECT COUNT(*) FROM weather_actuals").fetchone()[0]
    if n > 0:
        logger.info("Historical weather already seeded (%d rows). Skipping.", n)
        return
    logger.info("Seeding 90-day historical weather — runs once...")
    from datetime import date, timedelta
    weather = WeatherIngestor(db)
    end_date   = (date.today() - timedelta(days=1)).isoformat()
    start_date = (date.today() - timedelta(days=90)).isoformat()
    for city in Config.CITY_COORDS.keys():
        try:
            await weather.ingest_historical_data(city, start_date, end_date)
        except Exception as exc:
            logger.warning("Bootstrap failed for %s: %s", city, exc)
        await asyncio.sleep(0.3)
    logger.info("Historical bootstrap complete.")


async def _run_weather_loop(db: DWTraderDB) -> None:
    """Poll Open-Meteo forecasts for all cities every hour."""
    weather = WeatherIngestor(db)
    while True:
        try:
            for city in Config.CITY_COORDS.keys():
                await weather.ingest_forecast_data(city)
            logger.info("Weather ingestion complete for all cities.")
        except Exception as exc:
            logger.error("Weather ingestion error: %s", exc)
        await asyncio.sleep(3600)


async def run() -> None:
    print("=== BOT_RUNNER PROCESS STARTED ===", flush=True)
    env_mode = _env_mode()
    logger.info(
        "BotRunner starting | env=%s | trade_interval=%ds | monitor_interval=%ds",
        env_mode, TRADE_CYCLE_INTERVAL, MONITOR_INTERVAL,
    )

    # Single shared DB + pipeline — reused across WebSocket restarts.
    from src.ingest.ws_client import IngestionPipeline
    _db      = DWTraderDB()
    pipeline = IngestionPipeline(_db)
    ws_url   = (
        "wss://trading-api.kalshi.co/trade-api/ws/v2"
        if env_mode == "LIVE"
        else "wss://demo-api.kalshi.co/trade-api/ws/v2"
    )
    ws_task = _make_ws_task(pipeline, ws_url)

    # Health endpoint must be up before anything else so Azure Web App
    # health probes pass and the container isn't killed during startup.
    asyncio.create_task(_health_server(), name="health-server")

    # Seed historical data once so AR(1)/σ calibration has data from day one.
    await _bootstrap_historical(_db)

    # Launch weather polling as a fire-and-forget background task.
    asyncio.create_task(_run_weather_loop(_db), name="weather-ingest")

    last_trade        = 0.0
    last_monitor      = 0.0
    last_prune_date:   Optional[_date] = None
    last_outcome_date: Optional[_date] = None
    last_gumbel_date:  Optional[str]   = None

    while True:
        # ── Halt check ────────────────────────────────────────────────────────
        if HALT_PATH.exists():
            logger.warning(
                "HALT FLAG present (%s) — all trading paused. Remove file to resume.",
                HALT_PATH,
            )
            await asyncio.sleep(60)
            continue

        # Restart WebSocket if it died unexpectedly (reuses same pipeline/db)
        if ws_task.done():
            exc = ws_task.exception() if not ws_task.cancelled() else None
            logger.warning("WebSocket task ended (exc=%s) — restarting", exc)
            ws_task = _make_ws_task(pipeline, ws_url)

        now     = time.monotonic()
        now_utc = datetime.now(timezone.utc)

        # ── Gumbel experiment auto-schedule ──────────────────────────────────
        # Fires at PRUNE_HOUR_UTC (4 AM UTC = midnight US Eastern, after all
        # daily temp-market settlements) — same "end of day" gate as the DB prune.
        today_str = now_utc.strftime("%Y-%m-%d")
        if now_utc.hour == PRUNE_HOUR_UTC and today_str != last_gumbel_date:
            last_gumbel_date = today_str
            sched_mode = GUMBEL_SCHEDULE.get(today_str)
            if sched_mode:
                try:
                    _db.set_config("GUMBEL_MODE", sched_mode)
                    if Config.GUMBEL_MODE != sched_mode:
                        logger.info(
                            "Gumbel auto-schedule: %s -> %s for %s (4AM UTC)",
                            Config.GUMBEL_MODE, sched_mode, today_str,
                        )
                        Config.GUMBEL_MODE = sched_mode
                except Exception as exc:
                    logger.error("Gumbel auto-schedule failed: %s", exc)

        # ── Position monitor (every 2 min) ────────────────────────────────────
        if now - last_monitor >= MONITOR_INTERVAL:
            try:
                exits = await monitor_positions(env_mode)
                if exits:
                    logger.info("monitor_positions: %d position(s) exited", exits)
            except Exception as exc:
                logger.error("monitor_positions crashed: %s", exc, exc_info=True)
            last_monitor = time.monotonic()

        # ── Trade cycle (every 5 min) ─────────────────────────────────────────
        if now - last_trade >= TRADE_CYCLE_INTERVAL:
            try:
                await trade_cycle(env_mode)
            except Exception as exc:
                logger.error("trade_cycle crashed: %s", exc, exc_info=True)
            last_trade = time.monotonic()

        # ── Nightly outcome check (once per day at PRUNE_HOUR_UTC) ──────────────
        # Fires after Kalshi settles all daily temp markets (~midnight US Eastern).
        # Checks yesterday's fills, writes realized PnL + Brier scores to DB.
        if now_utc.hour == PRUNE_HOUR_UTC and now_utc.date() != last_outcome_date:
            yesterday = (now_utc.date() - timedelta(days=1)).isoformat()
            logger.info("Running nightly outcome check for %s ...", yesterday)
            try:
                summary = await _run_outcome_check(_db, yesterday, writeback=True)
                last_outcome_date = now_utc.date()
                logger.info(
                    "Outcome check complete | date=%s | settled=%d | pending=%d"
                    " | pnl=$%.2f | brier_updates=%d",
                    yesterday,
                    summary["settled"],
                    summary["pending"],
                    summary["total_pnl_cents"] / 100,
                    summary["written_back"],
                )
            except Exception as exc:
                logger.error("Outcome check failed: %s", exc, exc_info=True)

        # ── Nightly DB prune (once per day at PRUNE_HOUR_UTC) ─────────────────
        if now_utc.hour == PRUNE_HOUR_UTC and now_utc.date() != last_prune_date:
            logger.info("Starting nightly DB prune (PRUNE_HOUR_UTC=%d)...", PRUNE_HOUR_UTC)
            try:
                loop = asyncio.get_event_loop()
                stats = await loop.run_in_executor(None, _db_prune)
                last_prune_date = now_utc.date()
                logger.info(
                    "DB prune complete | scans -%s | orderbook -%s | weather -%s | size=%.1fMB",
                    stats["scans"]["deleted"],
                    stats["orderbook_events"]["deleted"],
                    stats["weather_data"]["deleted"],
                    stats.get("db_size_mb", 0),
                )
            except Exception as exc:
                logger.error("DB prune failed: %s", exc, exc_info=True)

        await asyncio.sleep(INNER_SLEEP)


if __name__ == "__main__":
    asyncio.run(run())
