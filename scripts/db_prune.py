"""
DB maintenance: prune high-volume tables to control growth.

  scans         — keep last 24h + any row referenced by decision_log
  orderbook_events — keep rolling window (default 48h)
  weather_data  — deduplicate on (city, target_date, hour)

Usage:
    python scripts/db_prune.py
    python scripts/db_prune.py --dry-run          # preview counts only
    python scripts/db_prune.py --orderbook-days 1
"""
import argparse
import logging
import os
import sys
from pathlib import Path

import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db.maintenance import prune

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("db_prune")


def _fmt(n: int) -> str:
    return f"{n:,}"


def dry_run_counts(orderbook_days: int) -> None:
    conn = pyodbc.connect(os.environ["AZURE_SQL_CONN_STR"], autocommit=False)
    try:
        c = conn.cursor()
        scans_total = c.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
        scans_would = c.execute("""
            SELECT COUNT(*) FROM scans
            WHERE timestamp < DATEADD(day, -1, GETDATE())
              AND scan_id NOT IN (
                  SELECT scan_id FROM decision_log WHERE scan_id IS NOT NULL
              )
        """).fetchone()[0]

        ob_total = c.execute("SELECT COUNT(*) FROM orderbook_events").fetchone()[0]
        ob_would = c.execute(
            f"SELECT COUNT(*) FROM orderbook_events "
            f"WHERE timestamp < DATEADD(day, -{orderbook_days}, GETDATE())"
        ).fetchone()[0]

        wd_total = c.execute("SELECT COUNT(*) FROM weather_data").fetchone()[0]
        wd_would = c.execute("""
            SELECT COUNT(*) FROM weather_data
            WHERE id NOT IN (SELECT MAX(id) FROM weather_data GROUP BY city, target_date, hour)
        """).fetchone()[0]

        size_row = c.execute("SELECT SUM(size) * 8.0 / 1024 FROM sys.database_files").fetchone()
        db_mb = round(float(size_row[0]), 2) if size_row and size_row[0] else 0.0

        log.info("[DRY RUN] scans: %s total, would delete %s, keep %s",
                 _fmt(scans_total), _fmt(scans_would), _fmt(scans_total - scans_would))
        log.info("[DRY RUN] orderbook_events: %s total, would delete %s, keep %s (window=%dd)",
                 _fmt(ob_total), _fmt(ob_would), _fmt(ob_total - ob_would), orderbook_days)
        log.info("[DRY RUN] weather_data: %s total, would delete %s duplicates",
                 _fmt(wd_total), _fmt(wd_would))
        log.info("[DRY RUN] DB size: %.2f MB", db_mb)
        log.info("[DRY RUN] no changes written.")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prune high-volume DB tables")
    parser.add_argument("--dry-run", action="store_true", help="Preview deletions without writing")
    parser.add_argument("--orderbook-days", type=int, default=2,
                        help="Days of orderbook_events to keep (default: 2)")
    args = parser.parse_args()

    if args.dry_run:
        dry_run_counts(args.orderbook_days)
    else:
        prune(orderbook_days=args.orderbook_days)
