"""
Trade outcome checker — run after each trading day to record settlement results,
compute realized PnL, and update Brier scores.

Usage (manual / backfill):
    python scripts/check_outcomes.py                   # yesterday's fills (default)
    python scripts/check_outcomes.py --date 2026-05-26 # specific date
    python scripts/check_outcomes.py --no-writeback    # dry-run, no DB writes

Called automatically by bot_runner.py every night at PRUNE_HOUR_UTC (4 AM UTC)
via run_check(db, target_date).
"""
import asyncio
import argparse
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Load .env locally (no-op when AZURE_SQL_CONN_STR is already in the environment)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from src.services.kalshi_client import client
from src.db.dwtrader import DWTraderDB


def _settle_date_from_ticker(ticker: str):
    """KXHIGHCHI-26APR21-T73 → '2026-04-21'  (the market's settlement date)."""
    m = re.match(r"KX(?:HIGH|TEMP)[A-Z]+-(\d{2}[A-Z]{3}\d{2})", ticker, re.IGNORECASE)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).upper(), "%y%b%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _get_fills(db: DWTraderDB, target_date: str) -> list:
    """Return all fills whose execution timestamp falls on target_date (Azure SQL)."""
    with db.get_connection() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT
                o.ticker,
                o.side,
                o.price_cents,
                o.qty,
                o.status             AS order_status,
                o.exchange_order_id,
                e.exchange_trade_id,
                e.price_cents        AS fill_price_cents,
                e.qty                AS fill_qty,
                e.timestamp          AS fill_time
            FROM orders o
            JOIN executions e ON e.order_id = o.order_id
            WHERE CAST(e.timestamp AS DATE) = ?
            ORDER BY e.timestamp
            """,
            (target_date,),
        )
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, row)) for row in c.fetchall()]


async def _check_settlement(ticker: str) -> dict:
    """Fetch settlement status from Kalshi REST."""
    try:
        return await client.get_market_settlement(ticker)
    except Exception as e:
        return {"status": "error", "result": None, "error": str(e)}


def _pnl_cents(fill: dict, settlement: dict) -> float:
    """
    Realized P&L in cents for one fill.
    Kalshi pays 100c per contract to the winning side.
    """
    side   = fill["side"]
    price  = fill["fill_price_cents"] or fill["price_cents"]
    qty    = fill["fill_qty"]         or fill["qty"]
    result = settlement.get("result")   # "yes" | "no" | None
    if result is None:
        return float("nan")
    won = (side == result)
    return ((100 - price) * qty) if won else (-price * qty)


async def run_check(
    db: DWTraderDB,
    target_date: str,
    writeback: bool = True,
) -> Dict[str, object]:
    """
    Check outcomes for all fills on target_date against the Kalshi REST API.

    For each settled fill:
      - Prints a P&L line to stdout
      - Writes actual_outcome + Brier score to predictions table
      - Writes realized_pnl_cents to the positions table

    Returns a summary dict:
        settled, pending, total_pnl_cents, written_back
    """
    fills = _get_fills(db, target_date)
    if not fills:
        print(f"No fills found for {target_date}.")
        return {"settled": 0, "pending": 0, "total_pnl_cents": 0.0, "written_back": 0}

    print(f"\n{'='*70}")
    print(f"TRADE OUTCOME REPORT  --  fills from {target_date}")
    print(f"{'='*70}")
    print(f"{'TICKER':<34} {'SIDE':<5} {'BUY@':<6} {'QTY':<4} {'RESULT':<10} {'P&L':>8}")
    print(f"{'-'*70}")

    total_pnl    = 0.0
    pending      = 0
    written_back = 0
    seen: dict   = {}   # ticker → settlement dict (fetch once per market)

    for fill in fills:
        ticker = fill["ticker"]
        if ticker not in seen:
            seen[ticker] = await _check_settlement(ticker)
        settlement = seen[ticker]

        pnl    = _pnl_cents(fill, settlement)
        result = settlement.get("result") or "PENDING"
        price  = fill["fill_price_cents"] or fill["price_cents"]
        qty    = fill["fill_qty"]         or fill["qty"]

        if result == "PENDING":
            pending += 1
            pnl_str = "pending"
        elif pnl != pnl:   # NaN guard
            pnl_str = "n/a"
        else:
            total_pnl += pnl
            pnl_str = f"{'+' if pnl >= 0 else ''}{pnl / 100:.2f}"

            if writeback and result in ("yes", "no"):
                actual_outcome = 1 if result == "yes" else 0
                settle_date    = _settle_date_from_ticker(ticker) or target_date

                # 1. Update predictions table (Brier score writeback)
                n = db.update_prediction_outcome(ticker, target_date, actual_outcome)
                if n == 0:
                    n = db.update_prediction_outcome(ticker, settle_date, actual_outcome)
                written_back += n

                # 2. Write realized PnL into positions table
                db.settle_position_with_outcome(ticker, yes_won=(result == "yes"))

                # 3. Write realized PnL into trade_attribution (source of truth for reports)
                if not (pnl != pnl):   # skip NaN
                    db.update_attribution_pnl(ticker, pnl)

        print(f"{ticker:<34} {fill['side']:<5} {price:<6} {qty:<4} {result:<10} {pnl_str:>8}")

    print(f"{'-'*70}")
    settled = len(fills) - pending
    print(f"  Settled : {settled}/{len(fills)}   Pending: {pending}")
    if settled:
        print(f"  Total P&L (settled): ${total_pnl / 100:+.2f}")
    if written_back:
        print(f"  Brier writeback: {written_back} prediction(s) updated")
    brier = db.get_brier_summary()
    if brier.get("n"):
        print(
            f"  Cumulative Brier: {brier['avg_brier']:.4f}"
            f"  (n={brier['n']}, target <0.10)"
        )
    print(f"{'='*70}\n")

    return {
        "settled":         settled,
        "pending":         pending,
        "total_pnl_cents": total_pnl,
        "written_back":    written_back,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Check trade settlement outcomes.")
    parser.add_argument(
        "--date",
        default=str(date.today() - timedelta(days=1)),
        help="YYYY-MM-DD of fills to check (default: yesterday)",
    )
    parser.add_argument(
        "--no-writeback",
        action="store_true",
        help="Dry-run — print results but skip all DB writes",
    )
    args = parser.parse_args()

    db = DWTraderDB()
    await run_check(db, args.date, writeback=not args.no_writeback)


if __name__ == "__main__":
    asyncio.run(main())
