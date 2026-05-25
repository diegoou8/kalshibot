"""Reconcile Kalshi portfolio fills and positions against the local Azure SQL DB."""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from src.services.kalshi_client import KalshiClient
from src.db.dwtrader import DWTraderDB

_ENV = "PAPER"
_GUMBEL = "half"


async def main() -> None:
    client = KalshiClient()
    db = DWTraderDB()

    positions = await client.get_portfolio_positions()
    print(f"\n--- Kalshi portfolio positions ({len(positions)}) ---")
    for p in positions:
        print(p)

    fills = await client.get_portfolio_fills()
    print(f"\n--- Kalshi portfolio fills ({len(fills)}) ---")
    for f in fills:
        print(f)

    missing = 0
    inserted = 0

    with db.get_connection() as conn:
        c = conn.cursor()
        for fill in fills:
            # Kalshi fill fields vary; use defensive .get() throughout
            exchange_trade_id = str(
                fill.get("trade_id") or fill.get("id") or fill.get("fill_id") or ""
            )
            exchange_order_id = str(fill.get("order_id") or "")

            # Check if this fill is already in executions
            c.execute(
                "SELECT execution_id FROM executions WHERE exchange_trade_id = ?",
                (exchange_trade_id,),
            )
            if c.fetchone():
                continue

            missing += 1

            fill_side = str(fill.get("side") or "yes")
            # Kalshi fill prices use *_dollars keys (e.g. yes_price_dollars = "0.3600")
            _price_key = "yes_price_dollars" if fill_side == "yes" else "no_price_dollars"
            _price_raw = fill.get(_price_key) or fill.get("yes_price") or fill.get("no_price") or fill.get("price") or 0
            fill_price = int(round(float(_price_raw) * 100)) if float(_price_raw or 0) < 2 else int(float(_price_raw or 0))
            # Qty is count_fp (a string like "2.00") or count
            fill_qty = int(float(fill.get("count_fp") or fill.get("count") or fill.get("qty") or 0))
            fill_ticker = str(fill.get("ticker") or "")

            # Look up our DB order by exchange_order_id
            db_order_id = None
            if exchange_order_id:
                c.execute(
                    "SELECT TOP 1 order_id FROM orders WHERE exchange_order_id = ?",
                    (exchange_order_id,),
                )
                row = c.fetchone()
                if row:
                    db_order_id = int(row[0])

            if db_order_id is not None:
                # Normal path: log execution which also upserts the position
                exec_id = db.log_execution(
                    order_id=db_order_id,
                    exchange_trade_id=exchange_trade_id,
                    ticker=fill_ticker,
                    side=fill_side,
                    price=fill_price,
                    qty=fill_qty,
                    environment=_ENV,
                    gumbel_mode=_GUMBEL,
                )
            else:
                # No matching order in DB — insert a synthetic position directly
                # so P&L tracking is still consistent
                c.execute(
                    "SELECT position_id, qty, avg_price_cents, cost_basis "
                    "FROM positions WHERE ticker = ? AND side = ? AND environment = ?",
                    (fill_ticker, fill_side, _ENV),
                )
                pos_row = c.fetchone()
                from datetime import datetime
                now = datetime.now().isoformat()
                if pos_row:
                    old_qty = int(pos_row[1])
                    new_qty = old_qty + fill_qty
                    new_avg = ((old_qty * float(pos_row[2])) + (fill_qty * fill_price)) / new_qty
                    new_cost = float(pos_row[3]) + (fill_price / 100.0) * fill_qty
                    c.execute(
                        "UPDATE positions SET qty=?, avg_price_cents=?, cost_basis=?, updated_at=? "
                        "WHERE position_id=?",
                        (new_qty, new_avg, new_cost, now, int(pos_row[0])),
                    )
                else:
                    cost_basis = (fill_price / 100.0) * fill_qty
                    c.execute(
                        "INSERT INTO positions "
                        "(ticker, side, qty, avg_price_cents, cost_basis, "
                        "realized_pnl_cents, unrealized_pnl_cents, updated_at, environment, gumbel_mode) "
                        "VALUES (?, ?, ?, ?, ?, 0.0, 0.0, ?, ?, ?)",
                        (fill_ticker, fill_side, fill_qty, fill_price, cost_basis,
                         now, _ENV, _GUMBEL),
                    )
                conn.commit()
                exec_id = None  # no executions row since we have no matching order_id

            if exec_id is not None:
                inserted += 1
            elif db_order_id is None and fill_qty > 0:
                inserted += 1  # synthetic position counts as inserted

    print(f"\n--- Reconcile summary ---")
    print(f"Kalshi positions : {len(positions)}")
    print(f"Kalshi fills     : {len(fills)}")
    print(f"Missing from DB  : {missing}")
    print(f"Inserted         : {inserted}")

    with db.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM positions ORDER BY updated_at DESC")
        cols = [d[0] for d in c.description]
        rows = c.fetchall()

    print(f"\n--- positions table ({len(rows)} rows) ---")
    print("  ".join(cols))
    for row in rows:
        print("  ".join(str(v) for v in row))


if __name__ == "__main__":
    asyncio.run(main())
