"""
Backtest / performance report — queries Azure SQL for historical fills and
computes Sharpe ratio, max drawdown, Brier by Gumbel mode, and P&L by city.

Usage:
    python scripts/backtest.py
    python scripts/backtest.py --days 60
    python scripts/backtest.py --mode half          # filter to one Gumbel mode
    python scripts/backtest.py --env LIVE           # live fills only
    python scripts/backtest.py --days 30 --csv out.csv
"""
import argparse
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from src.db.dwtrader import DWTraderDB


# ── Helpers ────────────────────────────────────────────────────────────────────

def _query(conn, sql: str, params: tuple = ()) -> List[dict]:
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _ds(val) -> str:
    if val is None:
        return ""
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    return str(val)[:10]


def _sharpe(daily_pnl: List[float]) -> Optional[float]:
    """Annualised Sharpe ratio from daily P&L values in cents."""
    if len(daily_pnl) < 2:
        return None
    n = len(daily_pnl)
    mean = sum(daily_pnl) / n
    var = sum((x - mean) ** 2 for x in daily_pnl) / (n - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std == 0:
        return None
    return (mean / std) * math.sqrt(252)


def _max_drawdown(cum_series: List[float]) -> float:
    """Maximum peak-to-trough drawdown in cents."""
    peak = float("-inf")
    max_dd = 0.0
    for val in cum_series:
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _sep(char: str = "-", width: int = 70) -> str:
    return char * width


# ── Report sections ────────────────────────────────────────────────────────────

def _section_fill_stats(conn, cutoff: str, mode_filter: Optional[str], env: str) -> None:
    if mode_filter:
        sql = """
            SELECT o.side,
                   COUNT(e.execution_id) AS n_fills,
                   AVG(CAST(e.price_cents AS FLOAT)) AS avg_price_cents
            FROM orders o
            JOIN executions e ON e.order_id = o.order_id
            WHERE CAST(o.created_at AS DATE) >= ?
              AND o.environment = ?
              AND o.gumbel_mode  = ?
            GROUP BY o.side
        """
        params: tuple = (cutoff, env, mode_filter)
    else:
        sql = """
            SELECT o.side,
                   COUNT(e.execution_id) AS n_fills,
                   AVG(CAST(e.price_cents AS FLOAT)) AS avg_price_cents
            FROM orders o
            JOIN executions e ON e.order_id = o.order_id
            WHERE CAST(o.created_at AS DATE) >= ?
              AND o.environment = ?
            GROUP BY o.side
        """
        params = (cutoff, env)

    rows = _query(conn, sql, params)
    print(_sep("="))
    hdr = "FILL STATISTICS"
    if mode_filter:
        hdr += f" (mode={mode_filter})"
    print(hdr)
    print(_sep("="))
    if not rows:
        print("  (no fills in window)")
        return
    total = sum(r["n_fills"] for r in rows)
    for r in rows:
        pct = r["n_fills"] / total * 100 if total else 0
        avg_p = r["avg_price_cents"] or 0
        print(
            f"  side={str(r['side']).ljust(4)}  "
            f"fills={r['n_fills']:>4}  "
            f"avg_price={avg_p:.1f}c  "
            f"({pct:.0f}%)"
        )
    print(f"  TOTAL  fills={total}")


def _section_experiment_runs(conn, cutoff: str, mode_filter: Optional[str]) -> None:
    sql = """
        SELECT run_date, gumbel_mode,
               total_trades, yes_trades, no_trades,
               avg_edge_cents, realized_pnl_cents, brier_score, n_settled
        FROM experiment_runs
        WHERE run_date >= ?
        ORDER BY run_date, gumbel_mode
    """
    rows = _query(conn, sql, (cutoff,))
    if mode_filter:
        rows = [r for r in rows if r["gumbel_mode"] == mode_filter]

    print(_sep("="))
    print("EXPERIMENT RUNS SUMMARY (from experiment_runs table)")
    print(_sep("="))
    if not rows:
        print("  (no experiment_runs data)")
        return

    by_mode: Dict[str, list] = {}
    for r in rows:
        gm = r["gumbel_mode"] or "unknown"
        by_mode.setdefault(gm, []).append(r)

    for gm, mode_rows in sorted(by_mode.items()):
        total_trades  = sum(r["total_trades"]       or 0 for r in mode_rows)
        total_settled = sum(r["n_settled"]          or 0 for r in mode_rows)
        total_pnl     = sum(r["realized_pnl_cents"] or 0 for r in mode_rows)
        brier_vals    = [r["brier_score"] for r in mode_rows if r["brier_score"] is not None]
        avg_brier     = sum(brier_vals) / len(brier_vals) if brier_vals else None
        print(f"\n  Mode: {gm.upper()}")
        print(f"    Days:          {len(mode_rows)}")
        print(f"    Total trades:  {total_trades}")
        print(f"    Settled:       {total_settled}")
        print(f"    Total PnL:     ${total_pnl/100:+.2f}")
        if avg_brier is not None:
            print(f"    Avg Brier:     {avg_brier:.4f}")


def _section_pnl_timeline(
    conn, cutoff: str, mode_filter: Optional[str], env: str, csv_path: Optional[str]
) -> None:
    """Daily PnL from trade_attribution with Sharpe + max drawdown."""
    if mode_filter:
        sql = """
            SELECT CAST(ta.recorded_at AS DATE) AS trade_date,
                   o.gumbel_mode,
                   SUM(ta.realized_pnl_cents) AS daily_pnl_cents
            FROM trade_attribution ta
            JOIN executions e ON e.execution_id = ta.execution_id
            JOIN orders o ON o.order_id = e.order_id
            WHERE CAST(ta.recorded_at AS DATE) >= ?
              AND o.environment = ?
              AND ta.realized_pnl_cents IS NOT NULL
              AND o.gumbel_mode = ?
            GROUP BY CAST(ta.recorded_at AS DATE), o.gumbel_mode
            ORDER BY trade_date
        """
        params: tuple = (cutoff, env, mode_filter)
    else:
        sql = """
            SELECT CAST(ta.recorded_at AS DATE) AS trade_date,
                   o.gumbel_mode,
                   SUM(ta.realized_pnl_cents) AS daily_pnl_cents
            FROM trade_attribution ta
            JOIN executions e ON e.execution_id = ta.execution_id
            JOIN orders o ON o.order_id = e.order_id
            WHERE CAST(ta.recorded_at AS DATE) >= ?
              AND o.environment = ?
              AND ta.realized_pnl_cents IS NOT NULL
            GROUP BY CAST(ta.recorded_at AS DATE), o.gumbel_mode
            ORDER BY trade_date
        """
        params = (cutoff, env)

    rows = _query(conn, sql, params)

    print(_sep("="))
    print("PnL TIMELINE (from trade_attribution)")
    print(_sep("="))

    if not rows:
        # fallback: positions table
        sql2 = """
            SELECT
                COUNT(*) AS n,
                SUM(CASE WHEN realized_pnl_cents IS NOT NULL
                         THEN realized_pnl_cents ELSE 0 END) AS total_pnl_cents
            FROM positions
            WHERE environment = ?
              AND status IN ('settled', 'closed')
        """
        fb = _query(conn, sql2, (env,))
        if fb and fb[0]["n"]:
            r = fb[0]
            print(f"  (trade_attribution empty — positions fallback)")
            print(f"  Settled positions: {r['n']}  total_pnl=${r['total_pnl_cents']/100:+.2f}")
        else:
            print("  (no settled data in trade_attribution or positions)")
        return

    daily_pnl: List[float] = [float(r["daily_pnl_cents"] or 0) for r in rows]
    cum = 0.0
    cum_series: List[float] = []
    for row in rows:
        cum += float(row["daily_pnl_cents"] or 0)
        cum_series.append(cum)
        gm   = str(row["gumbel_mode"] or "?").ljust(6)
        pnl_c = float(row["daily_pnl_cents"] or 0)
        print(f"  {_ds(row['trade_date'])}  mode={gm}  daily=${pnl_c/100:+7.2f}  cum=${cum/100:+7.2f}")

    sharpe = _sharpe(daily_pnl)
    max_dd = _max_drawdown(cum_series)
    print(_sep())
    print(f"  Total PnL:     ${cum/100:+.2f}")
    if sharpe is not None:
        print(f"  Sharpe (ann.): {sharpe:.3f}")
    else:
        print("  Sharpe: n/a (fewer than 2 days)")
    print(f"  Max Drawdown:  ${max_dd/100:.2f}")

    if csv_path:
        with open(csv_path, "w") as f:
            f.write("trade_date,gumbel_mode,daily_pnl_cents,daily_pnl_dollars\n")
            for r in rows:
                pnl_c = float(r["daily_pnl_cents"] or 0)
                f.write(
                    f"{_ds(r['trade_date'])},"
                    f"{r['gumbel_mode'] or ''},"
                    f"{pnl_c:.0f},"
                    f"{pnl_c/100:.4f}\n"
                )
        print(f"\n  CSV written to {csv_path}")


def _section_brier_by_mode(conn, cutoff: str, mode_filter: Optional[str], env: str) -> None:
    """Per-mode Brier from settled predictions (joined to orders for gumbel_mode)."""
    if mode_filter:
        sql = """
            SELECT o.gumbel_mode,
                   COUNT(*)                AS n_preds,
                   AVG(p.brier_score)      AS avg_brier,
                   MIN(p.brier_score)      AS min_brier,
                   MAX(p.brier_score)      AS max_brier
            FROM predictions p
            JOIN orders o ON o.ticker = p.ticker
                          AND CAST(o.created_at AS DATE) = CAST(p.trade_date AS DATE)
            WHERE CAST(p.trade_date AS DATE) >= ?
              AND p.actual_outcome IS NOT NULL
              AND p.brier_score    IS NOT NULL
              AND o.environment    = ?
              AND o.gumbel_mode    = ?
            GROUP BY o.gumbel_mode
            ORDER BY o.gumbel_mode
        """
        params: tuple = (cutoff, env, mode_filter)
    else:
        sql = """
            SELECT o.gumbel_mode,
                   COUNT(*)                AS n_preds,
                   AVG(p.brier_score)      AS avg_brier,
                   MIN(p.brier_score)      AS min_brier,
                   MAX(p.brier_score)      AS max_brier
            FROM predictions p
            JOIN orders o ON o.ticker = p.ticker
                          AND CAST(o.created_at AS DATE) = CAST(p.trade_date AS DATE)
            WHERE CAST(p.trade_date AS DATE) >= ?
              AND p.actual_outcome IS NOT NULL
              AND p.brier_score    IS NOT NULL
              AND o.environment    = ?
            GROUP BY o.gumbel_mode
            ORDER BY o.gumbel_mode
        """
        params = (cutoff, env)

    rows = _query(conn, sql, params)

    print(_sep("="))
    print("BRIER SCORE BY GUMBEL MODE (settled predictions)")
    print(_sep("="))
    if not rows:
        # fallback: all settled predictions without mode join
        sql2 = """
            SELECT COUNT(*) AS n, AVG(brier_score) AS avg_b
            FROM predictions
            WHERE CAST(trade_date AS DATE) >= ?
              AND actual_outcome IS NOT NULL
              AND brier_score IS NOT NULL
        """
        fb = _query(conn, sql2, (cutoff,))
        if fb and fb[0]["n"]:
            r = fb[0]
            print(f"  (all modes, no gumbel join)  n={r['n']}  avg_brier={r['avg_b']:.4f}")
        else:
            print("  (no settled predictions)")
        return

    for r in rows:
        gm  = str(r["gumbel_mode"] or "unknown").ljust(8)
        avg = r["avg_brier"] or 0
        flag = "  *** HIGH ***" if avg >= 0.25 else ""
        print(
            f"  {gm}  n={r['n_preds']:>4}  "
            f"avg={avg:.4f}  "
            f"min={r['min_brier']:.4f}  "
            f"max={r['max_brier']:.4f}{flag}"
        )


def _section_brier_by_city(conn, cutoff: str, mode_filter: Optional[str], env: str) -> None:
    """Per-city Brier from settled predictions."""
    if mode_filter:
        sql = """
            SELECT p.city,
                   COUNT(*)           AS n,
                   AVG(p.brier_score) AS avg_brier
            FROM predictions p
            JOIN orders o ON o.ticker = p.ticker
                          AND CAST(o.created_at AS DATE) = CAST(p.trade_date AS DATE)
            WHERE CAST(p.trade_date AS DATE) >= ?
              AND p.actual_outcome IS NOT NULL
              AND p.brier_score IS NOT NULL
              AND p.city IS NOT NULL
              AND o.environment = ?
              AND o.gumbel_mode  = ?
            GROUP BY p.city
            ORDER BY avg_brier DESC
        """
        params: tuple = (cutoff, env, mode_filter)
    else:
        sql = """
            SELECT p.city,
                   COUNT(*)           AS n,
                   AVG(p.brier_score) AS avg_brier
            FROM predictions p
            WHERE CAST(p.trade_date AS DATE) >= ?
              AND p.actual_outcome IS NOT NULL
              AND p.brier_score IS NOT NULL
              AND p.city IS NOT NULL
            GROUP BY p.city
            ORDER BY avg_brier DESC
        """
        params = (cutoff,)

    rows = _query(conn, sql, params)
    print(_sep("="))
    hdr = "BRIER BY CITY"
    if mode_filter:
        hdr += f" (mode={mode_filter})"
    print(hdr)
    print(_sep("="))
    if not rows:
        print("  (no settled predictions with city data)")
        return
    for r in rows:
        avg = r["avg_brier"] or 0
        flag = "  *** HIGH ***" if avg >= 0.25 else ""
        print(f"  {str(r['city'] or '?').ljust(6)}  n={r['n']:>4}  avg_brier={avg:.4f}{flag}")


def _section_pnl_by_city(conn, cutoff: str, mode_filter: Optional[str], env: str) -> None:
    """Per-city realized PnL from trade_attribution."""
    if mode_filter:
        sql = """
            SELECT ta.city,
                   COUNT(*)                       AS n_trades,
                   SUM(ta.realized_pnl_cents)     AS total_pnl_cents,
                   AVG(ta.expected_value_cents)   AS avg_ev_cents,
                   AVG(ta.fees_cents)             AS avg_fees_cents
            FROM trade_attribution ta
            JOIN executions e ON e.execution_id = ta.execution_id
            JOIN orders o ON o.order_id = e.order_id
            WHERE CAST(ta.recorded_at AS DATE) >= ?
              AND ta.city IS NOT NULL
              AND ta.realized_pnl_cents IS NOT NULL
              AND o.environment = ?
              AND o.gumbel_mode  = ?
            GROUP BY ta.city
            ORDER BY total_pnl_cents DESC
        """
        params: tuple = (cutoff, env, mode_filter)
    else:
        sql = """
            SELECT ta.city,
                   COUNT(*)                       AS n_trades,
                   SUM(ta.realized_pnl_cents)     AS total_pnl_cents,
                   AVG(ta.expected_value_cents)   AS avg_ev_cents,
                   AVG(ta.fees_cents)             AS avg_fees_cents
            FROM trade_attribution ta
            JOIN executions e ON e.execution_id = ta.execution_id
            JOIN orders o ON o.order_id = e.order_id
            WHERE CAST(ta.recorded_at AS DATE) >= ?
              AND ta.city IS NOT NULL
              AND ta.realized_pnl_cents IS NOT NULL
              AND o.environment = ?
            GROUP BY ta.city
            ORDER BY total_pnl_cents DESC
        """
        params = (cutoff, env)

    rows = _query(conn, sql, params)
    print(_sep("="))
    hdr = "PnL BY CITY"
    if mode_filter:
        hdr += f" (mode={mode_filter})"
    print(hdr)
    print(_sep("="))
    if not rows:
        print("  (no settled trade_attribution data — positions fallback not available by city)")
        return
    for r in rows:
        pnl_c  = float(r["total_pnl_cents"] or 0)
        ev_c   = float(r["avg_ev_cents"]     or 0)
        fees_c = float(r["avg_fees_cents"]   or 0)
        print(
            f"  {str(r['city'] or '?').ljust(6)}  "
            f"n={r['n_trades']:>4}  "
            f"pnl=${pnl_c/100:+7.2f}  "
            f"avg_ev={ev_c:.2f}c  "
            f"avg_fees={fees_c:.2f}c"
        )


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Kalshi bot backtest / performance report")
    ap.add_argument("--days", type=int, default=30,
                    help="Lookback window in days (default 30)")
    ap.add_argument("--mode", default=None,
                    help="Filter to Gumbel mode: half / none / full")
    ap.add_argument("--env",  default="PAPER",
                    help="Environment filter: PAPER or LIVE (default PAPER)")
    ap.add_argument("--csv",  default=None,
                    help="Write daily PnL time series to this CSV path")
    args = ap.parse_args()

    cutoff      = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    mode_filter = args.mode.lower() if args.mode else None
    env         = args.env.upper()

    db = DWTraderDB()

    print()
    print(_sep("=", 70))
    print("KALSHI BOT BACKTEST REPORT")
    print(f"  Window : last {args.days} days  (since {cutoff})")
    print(f"  Mode   : {mode_filter or 'all'}")
    print(f"  Env    : {env}")
    print(_sep("=", 70))

    with db.get_connection() as conn:
        print()
        _section_fill_stats(conn, cutoff, mode_filter, env)
        print()
        _section_experiment_runs(conn, cutoff, mode_filter)
        print()
        _section_pnl_timeline(conn, cutoff, mode_filter, env, args.csv)
        print()
        _section_brier_by_mode(conn, cutoff, mode_filter, env)
        print()
        _section_brier_by_city(conn, cutoff, mode_filter, env)
        print()
        _section_pnl_by_city(conn, cutoff, mode_filter, env)

    print()


if __name__ == "__main__":
    main()
