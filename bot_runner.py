"""
Continuous trading loop — Docker entry point for the trader service.

Two tasks run on independent schedules inside a single asyncio event loop:
  - trade_cycle()        every TRADE_CYCLE_INTERVAL_SECS  (default 300s / 5 min)
  - monitor_positions()  every MONITOR_INTERVAL_SECS       (default 120s / 2 min)

WebSocket ingestion starts once at startup as a long-lived background task.
The halt flag pauses trade_cycle and monitor_positions immediately.
"""
import asyncio
import logging
import os
import time
from pathlib import Path

from src.index import trade_cycle, monitor_positions
from src.config.env import Config
from src.db.dwtrader import DWTraderDB
from src.services.kalshi_client import client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("BotRunner")

# Absolute path — same anchor as RiskManager so the halt flag is always found.
_PROJECT_ROOT = Path(__file__).resolve().parent
HALT_PATH     = _PROJECT_ROOT / "data" / "halt.flag"

TRADE_CYCLE_INTERVAL = int(os.getenv("TRADE_CYCLE_INTERVAL_SECS", "300"))
MONITOR_INTERVAL     = int(os.getenv("MONITOR_INTERVAL_SECS", "120"))
INNER_SLEEP          = 10   # main-loop polling resolution (seconds)


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


async def run() -> None:
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

    last_trade   = 0.0
    last_monitor = 0.0

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

        now = time.monotonic()

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

        await asyncio.sleep(INNER_SLEEP)


if __name__ == "__main__":
    asyncio.run(run())
