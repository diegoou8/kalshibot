"""
PnL Decomposition Report
========================
Read-only analytics script. Decomposes realized trade results into:
  - Model alpha   : avg(predicted_p - market_implied_p) per city
  - Realized PnL  : outcome-weighted payoff in cents
  - Slippage      : fill_price vs mid at fill time
  - Fee drag      : sum of lvr_cents (Kalshi taker fee proxy)
  - Brier score   : per-city calibration

Run standalone:
    python analytics/pnl_decomposition.py

DB path resolved relative to project root (one level above this file).
"""

import sqlite3
import sys
import os
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional

# Allow imports from src/ if needed in the future
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

_DB_PATH = _PROJECT_ROOT / "data" / "DWTrader.db"

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_FILL_QUERY = """
SELECT
    e.execution_id,
    o.ticker,
    o.side,
    e.price_cents        AS fill_price,
    p.predicted_p,
    p.city,
    p.horizon_bin,
    p.actual_outcome,
    e.timestamp          AS fill_time,
    e.lvr_cents
FROM executions e
JOIN orders o ON o.exchange_order_id = e.exchange_trade_id
LEFT JOIN predictions p
       ON p.ticker     = o.ticker
      AND p.side       = o.side
      AND DATE(e.timestamp) = p.trade_date
      AND p.actual_outcome IS NOT NULL
WHERE e.environment = 'PAPER'
ORDER BY e.timestamp DESC
LIMIT 200
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _realized_pnl(fill_price: int, actual_outcome: int) -> float:
    """
    Kalshi settlement payoff in cents for one contract:
      WIN  (outcome=1): receive 100 - fill_price
      LOSS (outcome=0): lose fill_price paid
    """
    if actual_outcome == 1:
        return float(100 - fill_price)
    return float(-fill_price)


def _brier(predicted_p: float, actual_outcome: int) -> float:
    return (predicted_p - actual_outcome) ** 2


def _safe_mean(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _histogram(values: List[float], buckets: int = 5) -> str:
    """Return a compact ASCII histogram string."""
    if not values:
        return "(no data)"
    lo, hi = min(values), max(values)
    if lo == hi:
        return f"all values = {lo:.1f}c"
    width = (hi - lo) / buckets
    counts = [0] * buckets
    for v in values:
        idx = min(int((v - lo) / width), buckets - 1)
        counts[idx] = counts[idx] + 1
    lines = []
    for i, cnt in enumerate(counts):
        label = f"[{lo + i*width:+6.1f}, {lo + (i+1)*width:+6.1f})"
        bar = "#" * cnt
        lines.append(f"  {label} {bar} ({cnt})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main report
# ---------------------------------------------------------------------------

def run_report(db_path: str = str(_DB_PATH)) -> None:
    if not Path(db_path).exists():
        print(f"ERROR: database not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(_FILL_QUERY).fetchall()
    conn.close()

    total = len(rows)
    print(f"\n=== PnL Decomposition Report ===")
    print(f"Environment : PAPER")
    print(f"Rows loaded : {total} (capped at 200)")
    print(f"DB          : {db_path}\n")

    if total == 0:
        print("No PAPER executions found. Run paper trading first.")
        return

    # Accumulators keyed by city (or '__all__')
    model_alpha_by_city: Dict[str, List[float]] = defaultdict(list)
    pnl_by_city: Dict[str, List[float]] = defaultdict(list)
    brier_by_city: Dict[str, List[float]] = defaultdict(list)
    slippage_all: List[float] = []
    fee_drag_all: List[float] = []

    matched = 0  # rows where prediction join succeeded

    for row in rows:
        fill_price: int = row["fill_price"]
        predicted_p = row["predicted_p"]
        actual_outcome = row["actual_outcome"]
        city: str = row["city"] or "__unknown__"
        lvr_cents = row["lvr_cents"]

        # market implied probability = fill_price / 100
        market_implied_p: float = fill_price / 100.0

        # Slippage proxy: fill_price vs mid (we only have fill here)
        # When mid_at_fill is not available, slippage relative to market implied is 0
        # If the trade_attribution table is populated it will hold mid_at_fill_cents,
        # but from this join we just record fill_price deviation from 50c as a proxy.
        slippage_cents: float = float(fill_price) - 50.0  # directional bias proxy
        slippage_all.append(slippage_cents)

        if lvr_cents is not None:
            fee_drag_all.append(float(lvr_cents))

        if predicted_p is not None and actual_outcome is not None:
            matched += 1
            alpha = predicted_p - market_implied_p
            pnl = _realized_pnl(fill_price, actual_outcome)
            bs = _brier(predicted_p, actual_outcome)

            model_alpha_by_city[city].append(alpha)
            pnl_by_city[city].append(pnl)
            brier_by_city[city].append(bs)

            model_alpha_by_city["__all__"].append(alpha)
            pnl_by_city["__all__"].append(pnl)
            brier_by_city["__all__"].append(bs)

    # -----------------------------------------------------------------------
    # Print sections
    # -----------------------------------------------------------------------

    print("--- Model Alpha (predicted_p - market_implied_p) ---")
    print(f"{'City':<20} {'N':>5} {'Avg Alpha':>12} {'Notes'}")
    print("-" * 55)
    for city, alphas in sorted(model_alpha_by_city.items()):
        if city == "__all__":
            continue
        avg_a = _safe_mean(alphas)
        note = "EDGE" if avg_a is not None and avg_a > 0.02 else ("FADE" if avg_a is not None and avg_a < -0.02 else "flat")
        print(f"  {city:<18} {len(alphas):>5} {avg_a:>+11.4f}   {note}")

    agg = model_alpha_by_city.get("__all__", [])
    if agg:
        print(f"  {'ALL':<18} {len(agg):>5} {_safe_mean(agg):>+11.4f}   aggregate")
    print()

    print("--- Realized PnL by City (cents per contract) ---")
    print(f"{'City':<20} {'N':>5} {'Avg PnL':>10} {'Total PnL':>12}")
    print("-" * 55)
    for city, pnls in sorted(pnl_by_city.items()):
        if city == "__all__":
            continue
        avg_p = _safe_mean(pnls)
        total_p = sum(pnls)
        print(f"  {city:<18} {len(pnls):>5} {avg_p:>+9.1f}c  {total_p:>+10.1f}c")

    agg_pnl = pnl_by_city.get("__all__", [])
    if agg_pnl:
        print(f"  {'ALL':<18} {len(agg_pnl):>5} {_safe_mean(agg_pnl):>+9.1f}c  {sum(agg_pnl):>+10.1f}c")
    print()

    print("--- Slippage Distribution (fill_price - 50c proxy, cents) ---")
    print(_histogram(slippage_all))
    print(f"  Mean slippage proxy : {_safe_mean(slippage_all):>+.2f}c" if slippage_all else "  (no data)")
    print()

    print("--- Fee Drag (lvr_cents from executions) ---")
    if fee_drag_all:
        print(f"  Fills with fee data : {len(fee_drag_all)} / {total}")
        print(f"  Total fee drag      : {sum(fee_drag_all):>+.1f}c")
        print(f"  Avg fee per fill    : {_safe_mean(fee_drag_all):>+.2f}c")
    else:
        print("  No lvr_cents data yet. Populate executions.lvr_cents via log_execution_record.")
    print()

    print("--- Brier Score by City (lower = better calibration) ---")
    print(f"{'City':<20} {'N':>5} {'Avg Brier':>12} {'Calibrated?'}")
    print("-" * 55)
    for city, scores in sorted(brier_by_city.items()):
        if city == "__all__":
            continue
        avg_b = _safe_mean(scores)
        calibrated = "YES" if avg_b is not None and avg_b < 0.25 else "NO"
        print(f"  {city:<18} {len(scores):>5} {avg_b:>11.4f}   {calibrated}")

    agg_b = brier_by_city.get("__all__", [])
    if agg_b:
        avg_all_b = _safe_mean(agg_b)
        print(f"  {'ALL':<18} {len(agg_b):>5} {avg_all_b:>11.4f}   {'YES' if avg_all_b < 0.25 else 'NO'}")
    print()

    print("--- Coverage ---")
    print(f"  Total fills              : {total}")
    print(f"  Fills with prediction join: {matched}")
    print(f"  Unmatched (no prediction) : {total - matched}")
    if total - matched > matched:
        print("  WARNING: most fills have no matched prediction row.")
        print("  Ensure log_prediction() is called at order time with matching ticker+side+trade_date.")
    print()
    print("=== End of Report ===\n")


if __name__ == "__main__":
    db_override = sys.argv[1] if len(sys.argv) > 1 else str(_DB_PATH)
    run_report(db_path=db_override)
