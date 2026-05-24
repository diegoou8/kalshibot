"""
DB maintenance routines — called nightly by BotRunner and on-demand via
scripts/db_prune.py.

  scans           keep last 24h + any scan_id referenced by decision_log
  orderbook_events keep a rolling window (default 2 days)
  weather_data    deduplicate on (city, target_date, hour), keep latest
"""
import logging
import os
import pyodbc
from typing import Optional

log = logging.getLogger(__name__)


def _get_conn() -> pyodbc.Connection:
    return pyodbc.connect(os.environ["AZURE_SQL_CONN_STR"], autocommit=False)


def _fmt(n: int) -> str:
    return f"{n:,}"


def prune(orderbook_days: int = 2) -> dict:
    """
    Delete stale rows from high-volume tables.
    Returns a dict with before/after counts for each table.
    """
    conn = _get_conn()
    stats = {}

    try:
        c = conn.cursor()

        # ── 1. Scans ──────────────────────────────────────────────────────────
        before = c.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
        c.execute("""
            DELETE FROM scans
            WHERE timestamp < DATEADD(day, -1, GETDATE())
              AND scan_id NOT IN (
                  SELECT scan_id FROM decision_log WHERE scan_id IS NOT NULL
              )
        """)
        conn.commit()
        after = c.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
        stats["scans"] = {"before": before, "after": after, "deleted": before - after}
        log.info("scans: %s → %s (deleted %s)", _fmt(before), _fmt(after), _fmt(before - after))

        # ── 2. Orderbook events ───────────────────────────────────────────────
        before = c.execute("SELECT COUNT(*) FROM orderbook_events").fetchone()[0]
        c.execute(
            f"DELETE FROM orderbook_events WHERE timestamp < DATEADD(day, -{orderbook_days}, GETDATE())"
        )
        conn.commit()
        after = c.execute("SELECT COUNT(*) FROM orderbook_events").fetchone()[0]
        stats["orderbook_events"] = {"before": before, "after": after, "deleted": before - after}
        log.info("orderbook_events: %s → %s (deleted %s, window=%dd)",
                 _fmt(before), _fmt(after), _fmt(before - after), orderbook_days)

        # ── 3. Weather data duplicates ────────────────────────────────────────
        before = c.execute("SELECT COUNT(*) FROM weather_data").fetchone()[0]
        c.execute("""
            DELETE FROM weather_data
            WHERE id NOT IN (
                SELECT MAX(id) FROM weather_data GROUP BY city, target_date, hour
            )
        """)
        conn.commit()
        after = c.execute("SELECT COUNT(*) FROM weather_data").fetchone()[0]
        stats["weather_data"] = {"before": before, "after": after, "deleted": before - after}
        log.info("weather_data: %s → %s (deleted %s duplicates)",
                 _fmt(before), _fmt(after), _fmt(before - after))

        # ── 4. DB size (SQL Server) ───────────────────────────────────────────
        row = c.execute("SELECT SUM(size) * 8.0 / 1024 FROM sys.database_files").fetchone()
        db_mb = round(float(row[0]), 2) if row and row[0] else 0.0
        stats["db_size_mb"] = db_mb
        log.info("DB size: %.2f MB", db_mb)

    finally:
        conn.close()

    return stats
