"""
Trade outcome checker — run this the day after a trading session
to see which fills settled YES or NO and whether we won or lost.

Usage:
    python scripts/check_outcomes.py
    python scripts/check_outcomes.py --date 2026-04-20
"""
import asyncio
import argparse
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.kalshi_client import client
from src.db.dwtrader import DWTraderDB

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "DWTrader.db"


def _settle_date_from_ticker(ticker: str):
    """KXHIGHCHI-26APR21-T73 → '2026-04-21'  (the market's settlement date)."""
    m = re.match(r"KX(?:HIGH|TEMP)[A-Z]+-(\d{2}[A-Z]{3}\d{2})", ticker, re.IGNORECASE)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).upper(), "%y%b%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _get_fills(target_date: str):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            o.ticker,
            o.side,
            o.price_cents,
            o.qty,
            o.status          AS order_status,
            o.exchange_order_id,
            e.exchange_trade_id,
            e.price_cents     AS fill_price_cents,
            e.qty             AS fill_qty,
            e.timestamp       AS fill_time
        FROM orders o
        JOIN executions e ON e.order_id = o.order_id
        WHERE DATE(e.timestamp) = ?
        ORDER BY e.timestamp
        """,
        (target_date,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def _check_settlement(ticker: str):
    """Fetch settlement status from Kalshi REST."""
    try:
        result = await client.get_market_settlement(ticker)
        return result
    except Exception as e:
        return {"status": "error", "result": None, "error": str(e)}


def _pnl(fill: dict, settlement: dict) -> float:
    """
    Calculate realised P&L in cents for one fill.
    Kalshi pays 100c per contract to the winning side.
    """
    side = fill["side"]
    price = fill["fill_price_cents"] or fill["price_cents"]
    qty = fill["fill_qty"] or fill["qty"]
    result = settlement.get("result")  # "yes" | "no" | None

    if result is None:
        return float("nan")

    won = (side == result)
    if won:
        pnl_cents = (100 - price) * qty   # payout minus cost
    else:
        pnl_cents = -price * qty          # lost stake
    return pnl_cents


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=str(date.today()), help="YYYY-MM-DD of fills to check")
    parser.add_argument("--no-writeback", action="store_true", help="Skip writing outcomes to DB")
    args = parser.parse_args()

    fills = _get_fills(args.date)
    if not fills:
        print(f"No fills found for {args.date}.")
        return

    db = DWTraderDB()

    print(f"\n{'='*70}")
    print(f"TRADE OUTCOME REPORT -- fills from {args.date}")
    print(f"{'='*70}")
    print(f"{'TICKER':<32} {'SIDE':<5} {'BUY@':<6} {'QTY':<4} {'RESULT':<10} {'P&L':>8}")
    print(f"{'-'*70}")

    total_pnl = 0.0
    pending = 0
    written_back = 0

    # group fills by ticker so we only fetch settlement once per market
    seen_tickers: dict = {}

    for fill in fills:
        ticker = fill["ticker"]
        if ticker not in seen_tickers:
            seen_tickers[ticker] = await _check_settlement(ticker)
        settlement = seen_tickers[ticker]

        pnl    = _pnl(fill, settlement)
        result = settlement.get("result") or "PENDING"
        price  = fill["fill_price_cents"] or fill["price_cents"]

        if result == "PENDING":
            pending += 1
            pnl_str = "pending"
        elif pnl != pnl:
            pnl_str = "n/a"
        else:
            total_pnl += pnl
            pnl_str = f"{'+' if pnl >= 0 else ''}{pnl/100:.2f}"

            if not args.no_writeback and result in ("yes", "no"):
                actual_outcome = 1 if result == "yes" else 0
                # Use the market's settlement date (date in ticker), not fill date,
                # so that next-day fills on yesterday's market match correctly.
                n = db.update_prediction_outcome(ticker, args.date, actual_outcome)
                # Also try the fill date in case prediction was logged that day
                if n == 0:
                    settle_date = _settle_date_from_ticker(ticker) or args.date
                    n = db.update_prediction_outcome(ticker, settle_date, actual_outcome)
                written_back += n

        print(
            f"{ticker:<32} {fill['side']:<5} {price:<6} {fill['qty'] or fill['fill_qty']:<4} "
            f"{result:<10} {pnl_str:>8}"
        )

    print(f"{'-'*70}")
    settled = len(fills) - pending
    print(f"  Settled : {settled}/{len(fills)}   Pending: {pending}")
    if settled:
        print(f"  Total P&L (settled): ${total_pnl/100:+.2f}")
    if written_back:
        print(f"  Brier writeback: {written_back} predictions updated")
    brier = db.get_brier_summary()
    if brier.get("n"):
        print(f"  Cumulative Brier score: {brier['avg_brier']:.4f}  (n={brier['n']}, target <0.10)")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    asyncio.run(main())
