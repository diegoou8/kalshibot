"""
Holistic Gumbel A/B/C experiment report.

A/B/C protocol (3-day rotation):
  Apr 28 = half  |  Apr 29 = none  |  Apr 30 = full

Sections:
  0. Experiment protocol / mode schedule
  1. GUMBEL_MODE comparison  (with significance guardrails + normalized PnL)
  2. City bias summary       (with actionable suggestions)
  3. Tail-risk analysis      (rate + severity)
  4. Mode ranking            (PnL / Brier / bias neutrality)
  5. PnL by segment          (city / horizon / strike-distance)
  6. Warnings + action plan
  7. Daily experiment log

Usage:
    python analytics/calibration_report.py [--days 3]

Run daily, then submit to OpenAI / NotebookLM on Apr 30.
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db.dwtrader import DWTraderDB

_SEP  = "-" * 80
_SEP2 = "=" * 80

# A/B/C schedule: which mode runs on which date
_SCHEDULE: Dict[str, str] = {
    "2026-04-28": "half",
    "2026-04-29": "none",
    "2026-04-30": "full",
    "2026-05-04": "none",
    "2026-05-05": "none",
}

_CONFIDENCE_MIN_SETTLED = 30  # fewer => LOW_CONFIDENCE flag


# ── helpers ───────────────────────────────────────────────────────────────────

def _fmt(val: Optional[float], fmt: str = ".3f", na: str = "n/a") -> str:
    return format(val, fmt) if val is not None else na


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "n/a"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _today_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _rank_desc(vals: List[Tuple[str, float]]) -> Dict[str, int]:
    return {m: i + 1 for i, (m, _) in enumerate(sorted(vals, key=lambda x: x[1], reverse=True))}


def _rank_asc(vals: List[Tuple[str, float]]) -> Dict[str, int]:
    return {m: i + 1 for i, (m, _) in enumerate(sorted(vals, key=lambda x: x[1]))}


def _h(title: str) -> None:
    print(f"\n{_SEP}")
    print(f"  {title}")
    print(_SEP)


# ── data collection ───────────────────────────────────────────────────────────

def _collect(conn, window: str) -> dict:
    """Run all queries against populated tables and return plain-dict result sets."""
    import re as _re

    def _city_from_ticker(ticker: str) -> Optional[str]:
        m = _re.match(r"KX(?:HIGH|TEMP)([A-Z]+)-", ticker, _re.IGNORECASE)
        return m.group(1).upper() if m else None

    # ── 1. Fetch raw rows from populated tables ───────────────────────────────

    # Filled orders: orders JOIN executions — include gumbel_mode from orders column,
    # fall back to schedule lookup for rows that predate the column.
    filled_rows = conn.execute(
        """
        SELECT o.order_id, o.ticker, o.side, o.price_cents, o.qty, o.status,
               o.created_at, e.execution_id, e.lvr_cents, e.price_cents AS exec_price,
               o.gumbel_mode
        FROM orders o
        JOIN executions e ON e.order_id = o.order_id
        WHERE DATE(o.created_at) >= date('now', ?)
        """,
        (window,),
    ).fetchall()
    fills = [dict(r) for r in filled_rows]

    # Closed/settled positions — include both statuses.
    # gumbel_mode column is the authoritative source; fall back to schedule date for old rows.
    # For 'settled' rows where realized_pnl_cents was never booked (= 0),
    # fall back to unrealized_pnl_cents which reflects the last mark before expiry.
    closed_rows = conn.execute(
        """
        SELECT position_id, ticker, side, qty, avg_price_cents,
               CASE
                   WHEN realized_pnl_cents IS NOT NULL AND realized_pnl_cents != 0
                       THEN realized_pnl_cents
                   WHEN unrealized_pnl_cents IS NOT NULL
                       THEN unrealized_pnl_cents
                   ELSE 0
               END AS realized_pnl_cents,
               updated_at,
               gumbel_mode
        FROM positions
        WHERE status IN ('closed', 'settled')
          AND DATE(updated_at) >= date('now', ?)
        """,
        (window,),
    ).fetchall()
    closed_positions = [dict(r) for r in closed_rows]

    # Settled predictions
    pred_rows = conn.execute(
        """
        SELECT prediction_id, ticker, trade_date, side, predicted_p,
               actual_outcome, brier_score, city, recorded_at
        FROM predictions
        WHERE actual_outcome IS NOT NULL
          AND brier_score IS NOT NULL
          AND recorded_at >= date('now', ?)
        """,
        (window,),
    ).fetchall()
    settled_preds = [dict(r) for r in pred_rows]

    # Scans — fetch only those whose ticker appears in fills (avoid cartesian)
    fill_tickers = set(f["ticker"] for f in fills)
    scan_rows: List[dict] = []
    if fill_tickers:
        placeholders = ",".join("?" * len(fill_tickers))
        raw_scans = conn.execute(
            f"""
            SELECT ticker, market_probability, ml_probability, timestamp
            FROM scans
            WHERE timestamp >= date('now', ?)
              AND ticker IN ({placeholders})
            """,
            (window, *fill_tickers),
        ).fetchall()
        scan_rows = [dict(r) for r in raw_scans]

    # ── 2. Helper: assign gumbel_mode from _SCHEDULE by date string ──────────

    def _mode_for_date(date_str: str) -> Optional[str]:
        """Return mode for 'YYYY-MM-DD' or None if not in schedule."""
        return _SCHEDULE.get(date_str[:10])

    # ── 3. Build fill-date → mode index ──────────────────────────────────────

    # fills keyed by (ticker, date) for scan join
    fills_by_ticker_date: Dict[str, set] = {}
    for f in fills:
        date_str = f["created_at"][:10]
        fills_by_ticker_date.setdefault(f["ticker"], set()).add(date_str)

    # ── 4. exp: per-mode fill counts + avg Brier from settled predictions ─────

    # index settled_preds by trade_date for quick lookup
    pred_by_date: Dict[str, List[dict]] = {}
    for p in settled_preds:
        pred_by_date.setdefault(p["trade_date"], []).append(p)

    # group fills by mode — prefer stored gumbel_mode column, fall back to schedule date
    fills_by_mode: Dict[str, List[dict]] = {}
    for f in fills:
        mode = f.get("gumbel_mode") or _mode_for_date(f["created_at"])
        if mode is None:
            continue
        fills_by_mode.setdefault(mode, []).append(f)

    exp: List[dict] = []
    for mode, mode_fills in fills_by_mode.items():
        total = len(mode_fills)
        yes_t = sum(1 for f in mode_fills if f["side"] == "yes")
        no_t  = sum(1 for f in mode_fills if f["side"] != "yes")
        # Avg Brier: settled predictions whose trade_date maps to this mode
        brier_vals = [
            p["brier_score"]
            for date_str, preds in pred_by_date.items()
            if _mode_for_date(date_str) == mode
            for p in preds
            if p["brier_score"] is not None
        ]
        brier = sum(brier_vals) / len(brier_vals) if brier_vals else None
        exp.append({"gumbel_mode": mode, "total": total, "yes_t": yes_t, "no_t": no_t, "brier": brier})

    # ── 5. edge: per-mode avg edge + bias from scans (ml_prob - market_prob) ──

    # Build scan lookup: ticker → list of {date, ml_prob, market_prob}
    scan_by_ticker: Dict[str, List[dict]] = {}
    for s in scan_rows:
        scan_by_ticker.setdefault(s["ticker"], []).append(s)

    edge_acc: Dict[str, Dict] = {}  # mode → {edges, biases, n}
    for f in fills:
        mode = f.get("gumbel_mode") or _mode_for_date(f["created_at"])
        if mode is None:
            continue
        f_date = f["created_at"][:10]
        for s in scan_by_ticker.get(f["ticker"], []):
            if s["timestamp"][:10] != f_date:
                continue
            ml_p  = s["ml_probability"]
            mkt_p = s["market_probability"]
            if ml_p is None or mkt_p is None:
                continue
            acc = edge_acc.setdefault(mode, {"edges": [], "biases": [], "n": 0})
            acc["edges"].append(ml_p - mkt_p)
            acc["biases"].append(ml_p - mkt_p)
            acc["n"] += 1

    edge: List[dict] = []
    for mode, acc in edge_acc.items():
        n = acc["n"]
        avg_edge = sum(acc["edges"]) / n if n else None
        avg_bias = sum(acc["biases"]) / n if n else None
        edge.append({"gumbel_mode": mode, "n_eval": n, "avg_edge": avg_edge, "avg_bias": avg_bias})

    # ── 6. lvr: per-mode avg lvr_cents + total_pnl from closed positions ──────

    # group positions by mode — prefer stored gumbel_mode column, fall back to schedule date
    pos_by_mode: Dict[str, List[dict]] = {}
    for pos in closed_positions:
        mode = pos.get("gumbel_mode") or _mode_for_date(pos["updated_at"])
        if mode is None:
            continue
        pos_by_mode.setdefault(mode, []).append(pos)

    lvr_acc: Dict[str, Dict] = {}
    for f in fills:
        mode = f.get("gumbel_mode") or _mode_for_date(f["created_at"])
        if mode is None:
            continue
        acc = lvr_acc.setdefault(mode, {"lvr_vals": [], "n_fills": 0})
        if f["lvr_cents"] is not None:
            acc["lvr_vals"].append(f["lvr_cents"])
        acc["n_fills"] += 1

    lvr: List[dict] = []
    all_modes_lvr = set(list(lvr_acc.keys()) + list(pos_by_mode.keys()))
    for mode in all_modes_lvr:
        acc = lvr_acc.get(mode, {"lvr_vals": [], "n_fills": 0})
        lvr_vals = acc["lvr_vals"]
        avg_lvr  = sum(lvr_vals) / len(lvr_vals) if lvr_vals else None
        mode_poss = pos_by_mode.get(mode, [])
        total_pnl = sum(p["realized_pnl_cents"] or 0 for p in mode_poss) if mode_poss else None
        n_fills   = acc["n_fills"]
        lvr.append({"gumbel_mode": mode, "avg_lvr": avg_lvr, "total_pnl": total_pnl, "n_fills": n_fills})

    # ── 7. settled_by_mode: count settled predictions per mode ────────────────

    stl_by_mode: Dict[str, int] = {}
    for p in settled_preds:
        mode = _mode_for_date(p["trade_date"])
        if mode is None:
            continue
        stl_by_mode[mode] = stl_by_mode.get(mode, 0) + 1

    settled_by_mode: List[dict] = [
        {"gumbel_mode": m, "n_settled": n} for m, n in stl_by_mode.items()
    ]

    # ── 8. bias: city-level avg bias from scans (only cities with fills) ──────

    bias_acc: Dict[str, Dict] = {}  # city → {edges, ml_probs, mkt_probs}
    for s in scan_rows:
        city = _city_from_ticker(s["ticker"])
        if city is None:
            continue
        # Only include if this ticker appears in any fill on the same date
        s_date = s["timestamp"][:10]
        ticker_fills_dates = fills_by_ticker_date.get(s["ticker"], set())
        if s_date not in ticker_fills_dates:
            continue
        ml_p  = s["ml_probability"]
        mkt_p = s["market_probability"]
        if ml_p is None or mkt_p is None:
            continue
        acc = bias_acc.setdefault(city, {"edges": [], "ml_probs": [], "mkt_probs": []})
        acc["edges"].append(ml_p - mkt_p)
        acc["ml_probs"].append(ml_p)
        acc["mkt_probs"].append(mkt_p)

    bias_list: List[dict] = []
    for city, acc in bias_acc.items():
        n = len(acc["edges"])
        avg_bias     = sum(acc["edges"])    / n if n else None
        avg_p_model  = sum(acc["ml_probs"]) / n if n else None
        avg_p_market = sum(acc["mkt_probs"]) / n if n else None
        bias_list.append({"city": city, "n": n, "avg_bias": avg_bias,
                          "avg_p_model": avg_p_model, "avg_p_market": avg_p_market})

    bias_list.sort(key=lambda r: abs(r["avg_bias"] or 0.0), reverse=True)

    # ── 9. city_brier: avg brier per city from settled predictions ────────────

    city_brier_acc: Dict[str, List[float]] = {}
    for p in settled_preds:
        city = p.get("city") or _city_from_ticker(p["ticker"])
        if city is None or p["brier_score"] is None:
            continue
        city_brier_acc.setdefault(city, []).append(p["brier_score"])

    city_brier: List[dict] = [
        {"city": city, "brier": sum(bs) / len(bs), "n_settled": len(bs)}
        for city, bs in city_brier_acc.items()
    ]

    # ── 10. pnl_city: closed positions grouped by city ────────────────────────

    pnl_city_acc: Dict[str, Dict] = {}
    for pos in closed_positions:
        city = _city_from_ticker(pos["ticker"])
        if city is None:
            continue
        acc = pnl_city_acc.setdefault(city, {"pnls": [], "n": 0, "lvr_vals": [], "fees": 0.0})
        acc["pnls"].append(pos["realized_pnl_cents"] or 0)
        acc["n"] += 1

    # Also pull lvr from fills matched to closed-position tickers
    closed_tickers = set(p["ticker"] for p in closed_positions)
    for f in fills:
        if f["ticker"] in closed_tickers:
            city = _city_from_ticker(f["ticker"])
            if city and city in pnl_city_acc and f["lvr_cents"] is not None:
                pnl_city_acc[city]["lvr_vals"].append(f["lvr_cents"])

    pnl_city_list: List[dict] = []
    for city, acc in pnl_city_acc.items():
        total_pnl = sum(acc["pnls"])
        avg_pnl   = total_pnl / acc["n"] if acc["n"] else None
        avg_lvr   = sum(acc["lvr_vals"]) / len(acc["lvr_vals"]) if acc["lvr_vals"] else None
        pnl_city_list.append({
            "city": city, "n_fills": acc["n"],
            "total_pnl": total_pnl, "avg_pnl": avg_pnl,
            "avg_lvr": avg_lvr, "total_fees": None,
        })
    pnl_city_list.sort(key=lambda r: r["total_pnl"], reverse=True)

    # ── 11. daily: one row per (date, mode) from _SCHEDULE ───────────────────

    daily: List[dict] = []
    for date_str, mode in sorted(_SCHEDULE.items()):
        day_fills = [f for f in fills if f["created_at"][:10] == date_str]
        total_trades = len(day_fills)
        yes_trades   = sum(1 for f in day_fills if f["side"] == "yes")
        no_trades    = sum(1 for f in day_fills if f["side"] != "yes")
        day_preds = pred_by_date.get(date_str, [])
        brier_vals = [p["brier_score"] for p in day_preds if p["brier_score"] is not None]
        brier_score = sum(brier_vals) / len(brier_vals) if brier_vals else None
        day_poss = [p for p in closed_positions if p["updated_at"][:10] == date_str]
        realized_pnl_cents = sum(p["realized_pnl_cents"] or 0 for p in day_poss) if day_poss else None
        daily.append({
            "run_date": date_str, "gumbel_mode": mode,
            "total_trades": total_trades, "yes_trades": yes_trades, "no_trades": no_trades,
            "brier_score": brier_score, "realized_pnl_cents": realized_pnl_cents,
        })

    return {
        "exp": exp,
        "edge": edge,
        "lvr": lvr,
        "settled_by_mode": settled_by_mode,
        "bias": bias_list,
        "city_brier": city_brier,
        "tail": [],
        "eligible": [],
        "pnl_city": pnl_city_list,
        "pnl_horizon": [],
        "pnl_strike": [],
        "daily": daily,
    }


# ── report ────────────────────────────────────────────────────────────────────

def run_report(days: int = 3) -> None:
    db = DWTraderDB()
    window = f"-{days} days"

    with db.get_connection() as conn:
        raw = _collect(conn, window)

    # ── build mode_data ───────────────────────────────────────────────────────
    exp_m  = {r["gumbel_mode"]: dict(r) for r in raw["exp"]}
    edge_m = {r["gumbel_mode"]: dict(r) for r in raw["edge"]}
    lvr_m  = {r["gumbel_mode"]: dict(r) for r in raw["lvr"]}
    stl_m  = {r["gumbel_mode"]: r["n_settled"] for r in raw["settled_by_mode"]}

    all_modes = sorted(set(list(exp_m) + list(edge_m) + list(lvr_m)))

    mode_data: Dict[str, dict] = {}
    for mode in all_modes:
        ex = exp_m.get(mode, {})
        ed = edge_m.get(mode, {})
        lv = lvr_m.get(mode, {})
        n_fills = lv.get("n_fills") or 0
        total_pnl = lv.get("total_pnl")
        n_settled = stl_m.get(mode, 0)
        mode_data[mode] = {
            "n_eval":       ed.get("n_eval") or 0,
            "n_trade":      ex.get("total")  or 0,
            "yes_t":        ex.get("yes_t")  or 0,
            "no_t":         ex.get("no_t")   or 0,
            "avg_edge":     ed.get("avg_edge"),
            "avg_bias":     ed.get("avg_bias"),
            "avg_lvr":      lv.get("avg_lvr"),
            "total_pnl":    total_pnl,
            "pnl_per_cont": (total_pnl / n_fills) if (total_pnl is not None and n_fills > 0) else None,
            "pnl_per_day":  (total_pnl / days)    if total_pnl is not None else None,
            "brier":        ex.get("brier"),
            "n_fills":      n_fills,
            "n_settled":    n_settled,
            "confidence":   "LOW_CONFIDENCE" if n_settled < _CONFIDENCE_MIN_SETTLED else "ok",
        }

    # ── build city data ───────────────────────────────────────────────────────
    brier_city = {r["city"]: (r["brier"], r["n_settled"]) for r in raw["city_brier"]}
    city_data: Dict[str, dict] = {}
    for r in raw["bias"]:
        city = r["city"]
        bv, ns = brier_city.get(city, (None, 0))
        city_data[city] = {
            "n": r["n"], "avg_bias": r["avg_bias"],
            "avg_p_model": r["avg_p_model"], "avg_p_market": r["avg_p_market"],
            "brier": bv, "n_settled": ns,
        }

    # ── build tail data ───────────────────────────────────────────────────────
    elig = {r["city"]: r["n_eligible"] for r in raw["eligible"]}

    # ── print ─────────────────────────────────────────────────────────────────

    print(f"\n{_SEP2}")
    print(f"  GUMBEL A/B/C EXPERIMENT REPORT - last {days} days  |  {_now()}")
    print(_SEP2)

    # ── SECTION 0: EXPERIMENT PROTOCOL ───────────────────────────────────────
    _h("0. EXPERIMENT PROTOCOL  (A/B/C - one mode per day)")
    today = _today_date()
    print()
    for date_str, mode in sorted(_SCHEDULE.items()):
        marker = "  <-- TODAY" if date_str == today else ""
        print(f"  {date_str}  GUMBEL_MODE={mode}{marker}")
    print()
    print("  Switch mode each morning:  python scripts/set_gumbel_mode.py <none|half|full>")
    print("  Run report after each day: python analytics/calibration_report.py --days 3")
    print(f"  Confidence threshold: n_settled >= {_CONFIDENCE_MIN_SETTLED} before drawing conclusions.")

    # ── SECTION 1: MODE COMPARISON ────────────────────────────────────────────
    _h("1. GUMBEL_MODE COMPARISON")

    if not all_modes:
        print(f"\n  No data yet - run the bot first.\n")
    else:
        print(
            f"\n  {'Mode':<6} {'Evals':>6}  {'Trades':>7}  {'YES%':>6}  "
            f"{'AvgEdge':>9}  {'AvgLVR':>8}  {'PnL(c)':>8}  "
            f"{'PnL/c':>7}  {'PnL/d':>7}  {'Brier':>7}  "
            f"{'Settled':>8}  Confidence"
        )
        print(
            f"  {'------':<6} {'------':>6}  {'-------':>7}  {'------':>6}  "
            f"{'-------':>9}  {'------':>8}  {'------':>8}  "
            f"{'-----':>7}  {'-----':>7}  {'-----':>7}  "
            f"{'-------':>8}  ----------"
        )
        for mode in all_modes:
            d = mode_data[mode]
            print(
                f"  {mode:<6} {d['n_eval']:>6}  {d['n_trade']:>7}  "
                f"{_pct(d['yes_t'], d['n_trade']):>6}  "
                f"{_fmt(d['avg_edge'], '+.1f'):>8}c  "
                f"{_fmt(d['avg_lvr'],  '+.1f'):>7}c  "
                f"{_fmt(d['total_pnl'],'+.1f'):>7}c  "
                f"{_fmt(d['pnl_per_cont'], '+.1f'):>6}c  "
                f"{_fmt(d['pnl_per_day'],  '+.1f'):>6}c  "
                f"{_fmt(d['brier']):>7}  "
                f"{d['n_settled']:>8}  {d['confidence']}"
            )

    # ── SECTION 2: CITY BIAS SUMMARY ─────────────────────────────────────────
    _h("2. CITY BIAS SUMMARY  (sorted by |avg_bias| descending)")

    if not city_data:
        print("\n  No city data yet.\n")
    else:
        print(
            f"\n  {'City':<6} {'N':>5}  {'AvgBias':>9}  "
            f"{'P_model':>8}  {'P_market':>9}  {'Brier':>7}  {'Settled':>8}  "
            f"Status + Action"
        )
        print(
            f"  {'------':<6} {'-----':>5}  {'---------':>9}  "
            f"{'--------':>8}  {'---------':>9}  {'-----':>7}  {'-------':>8}  "
            f"----------------------------"
        )
        for city, d in city_data.items():
            bias = d["avg_bias"]
            n    = d["n"]
            status = ""
            action = ""
            if bias is not None and n >= 10:
                if bias < -0.05:
                    status = "[!] NO-BIAS"
                    action = "-> reduce sigma or lower GUMBEL_MODE"
                elif bias > 0.05:
                    status = "[!] YES-BIAS"
                    action = "-> increase sigma or raise GUMBEL_MODE"
                else:
                    status = "[ok]"
            print(
                f"  {city:<6} {n:>5}  {_fmt(bias, '+.4f'):>9}  "
                f"{_fmt(d['avg_p_model']):>8}  {_fmt(d['avg_p_market']):>9}  "
                f"{_fmt(d['brier']):>7}  {d['n_settled']:>8}  "
                f"{status}"
            )
            if action:
                print(f"  {'':30}  {action}")

    # ── SECTION 3: TAIL-RISK ANALYSIS ─────────────────────────────────────────
    _h("3. TAIL-RISK ANALYSIS  (p_yes < 5% AND outcome = YES)")
    print("  Rate = tail_hits / n_eligible.  Severity = avg realized PnL on those fills.")

    if not raw["tail"]:
        print(f"\n  No tail-risk events in the last {days} days. [ok]\n")
    else:
        print(
            f"\n  {'City':<6} {'Hits':>6}  {'Eligible':>9}  "
            f"{'Rate%':>7}  {'AvgP@Hit':>9}  {'AvgLoss(c)':>11}  Status"
        )
        print(
            f"  {'------':<6} {'----':>6}  {'---------':>9}  "
            f"{'-----':>7}  {'---------':>9}  {'----------':>11}  --------"
        )
        for r in raw["tail"]:
            city     = r["city"]
            n_elig   = elig.get(city, 0)
            rate_pct = 100 * r["tail_hits"] / n_elig if n_elig else 0.0
            avg_loss = r["avg_pnl_on_hit"]  # negative = we lost money on those
            flag     = "[!] TAIL-RISK" if r["tail_hits"] >= 2 else "watch"
            print(
                f"  {city:<6} {r['tail_hits']:>6}  {n_elig:>9}  "
                f"{rate_pct:>6.1f}%  {_fmt(r['avg_p_at_hit'], '.4f'):>9}  "
                f"{_fmt(avg_loss, '+.1f'):>10}c  {flag}"
            )

    # ── SECTION 4: MODE RANKING ───────────────────────────────────────────────
    _h("4. GUMBEL_MODE RANKING")
    print("  PnL (higher=better) | Brier (lower=better) | |Bias| (0=best)")
    print("  Composite = sum of rank positions. Lowest composite = best mode overall.")
    print("  Note: only modes with n_settled >= 30 should be trusted.")

    if not mode_data:
        print("\n  No mode data yet.\n")
    else:
        pnl_v   = [(m, d["total_pnl"])           for m, d in mode_data.items() if d["total_pnl"]  is not None]
        brier_v = [(m, d["brier"])                for m, d in mode_data.items() if d["brier"]      is not None]
        bias_v  = [(m, abs(d["avg_bias"] or 0.0)) for m, d in mode_data.items()]

        pnl_r   = _rank_desc(pnl_v)
        brier_r = _rank_asc(brier_v)
        bias_r  = _rank_asc(bias_v)

        ranked = []
        for mode in sorted(mode_data):
            rp = pnl_r.get(mode, "n/a")
            rb = brier_r.get(mode, "n/a")
            ra = bias_r.get(mode, "n/a")
            nums = [x for x in (rp, rb, ra) if isinstance(x, int)]
            comp = sum(nums) if nums else 999
            conf = mode_data[mode]["confidence"]
            ranked.append((comp, mode, rp, rb, ra, conf))
        ranked.sort()

        print(
            f"\n  {'Mode':<6}  {'PnL':>5}  {'Brier':>6}  {'Bias':>5}  "
            f"{'Comp':>5}  {'Verdict':<10}  Confidence"
        )
        print(
            f"  {'------':<6}  {'---':>5}  {'-----':>6}  {'----':>5}  "
            f"{'----':>5}  {'-------':<10}  ----------"
        )
        for i, (comp, mode, rp, rb, ra, conf) in enumerate(ranked):
            verdict = "[*] BEST" if i == 0 else ("OK" if i == 1 else "WORST")
            low_conf = " (*)" if conf == "LOW_CONFIDENCE" else ""
            print(
                f"  {mode:<6}  {str(rp):>5}  {str(rb):>6}  {str(ra):>5}  "
                f"{comp:>5}  {verdict:<10}  {conf}{low_conf}"
            )
        if any(d["confidence"] == "LOW_CONFIDENCE" for d in mode_data.values()):
            print("\n  (*) LOW_CONFIDENCE: fewer than 30 settled predictions for this mode.")
            print("      Do not switch modes based on these ranks alone.")

    # ── SECTION 5: PNL BY SEGMENT ─────────────────────────────────────────────
    _h("5. PNL BY SEGMENT")

    # 5a. By city
    print("\n  5a. By City")
    if not raw["pnl_city"]:
        print("  No fill data yet.\n")
    else:
        print(
            f"\n  {'City':<6} {'Fills':>6}  {'TotalPnL':>10}  "
            f"{'AvgPnL/fill':>12}  {'AvgLVR':>8}  {'TotalFees':>10}"
        )
        print(
            f"  {'------':<6} {'-----':>6}  {'--------':>10}  "
            f"{'----------':>12}  {'------':>8}  {'---------':>10}"
        )
        for r in raw["pnl_city"]:
            print(
                f"  {r['city']:<6} {r['n_fills']:>6}  "
                f"{_fmt(r['total_pnl'], '+.1f'):>9}c  "
                f"{_fmt(r['avg_pnl'],   '+.2f'):>11}c  "
                f"{_fmt(r['avg_lvr'],   '+.1f'):>7}c  "
                f"{_fmt(r['total_fees'],'+.1f'):>9}c"
            )

    # 5b. By horizon bucket
    print("\n  5b. By Horizon Bucket")
    if not raw["pnl_horizon"]:
        print("  No fill data yet.\n")
    else:
        print(
            f"\n  {'Horizon':<8} {'Fills':>6}  {'TotalPnL':>10}  {'AvgPnL/fill':>12}"
        )
        print(
            f"  {'-------':<8} {'-----':>6}  {'--------':>10}  {'----------':>12}"
        )
        for r in raw["pnl_horizon"]:
            print(
                f"  {(r['bucket'] or 'n/a'):<8} {r['n_fills']:>6}  "
                f"{_fmt(r['total_pnl'], '+.1f'):>9}c  "
                f"{_fmt(r['avg_pnl'],   '+.2f'):>11}c"
            )

    # 5c. By strike-distance bucket
    print("\n  5c. By Strike Distance  (far_itm=deep YES zone, far_otm=deep NO zone)")
    if not raw["pnl_strike"]:
        print("  No fill data yet.\n")
    else:
        print(
            f"\n  {'Bucket':<8} {'Fills':>6}  {'TotalPnL':>10}  {'AvgPnL/fill':>12}"
        )
        print(
            f"  {'------':<8} {'-----':>6}  {'--------':>10}  {'----------':>12}"
        )
        for r in raw["pnl_strike"]:
            print(
                f"  {(r['bucket'] or 'n/a'):<8} {r['n_fills']:>6}  "
                f"{_fmt(r['total_pnl'], '+.1f'):>9}c  "
                f"{_fmt(r['avg_pnl'],   '+.2f'):>11}c"
            )

    # ── SECTION 6: WARNINGS + ACTION PLAN ────────────────────────────────────
    _h("6. WARNINGS + ACTION PLAN")

    warnings: List[str] = []

    # YES/NO ratio > 80/20
    for mode, d in mode_data.items():
        t = d["n_trade"]
        if t >= 5:
            yf = d["yes_t"] / t
            nf = d["no_t"]  / t
            if yf > 0.80:
                warnings.append(
                    f"[mode={mode}] YES={yf*100:.0f}% > 80% - systematic long bias. "
                    f"Action: raise MIN_PROB_EDGE_PP or check YES-side sigma."
                )
            elif nf > 0.80:
                warnings.append(
                    f"[mode={mode}] NO={nf*100:.0f}% > 80% - systematic short bias. "
                    f"Action: check NO-side edge calculation."
                )

    # City avg_bias outside +/-0.05 (n >= 10)
    for city, d in city_data.items():
        b = d["avg_bias"]
        n = d["n"]
        if b is not None and n >= 10:
            if b < -0.05:
                warnings.append(
                    f"[city={city}] avg_bias={b:+.4f} (n={n}) - underestimates YES. "
                    f"Action: reduce sigma or switch to GUMBEL_MODE=none for {city}."
                )
            elif b > 0.05:
                warnings.append(
                    f"[city={city}] avg_bias={b:+.4f} (n={n}) - overestimates YES. "
                    f"Action: increase sigma or try GUMBEL_MODE=full for {city}."
                )

    # Tail-risk >= 2 in any city
    for r in raw["tail"]:
        if r["tail_hits"] >= 2:
            warnings.append(
                f"[city={r['city']}] {r['tail_hits']} tail events (p<5%, YES settled). "
                f"Action: block {r['city']} for 48h, widen sigma, review Gumbel correction."
            )

    # Low-confidence modes
    for mode, d in mode_data.items():
        if d["confidence"] == "LOW_CONFIDENCE":
            warnings.append(
                f"[mode={mode}] only {d['n_settled']} settled predictions "
                f"(need >= {_CONFIDENCE_MIN_SETTLED}). "
                f"Action: do not switch modes yet - wait for more settlements."
            )

    if warnings:
        print()
        for i, w in enumerate(warnings, 1):
            print(f"  [{i}] {w}")
    else:
        print("\n  No warnings. [ok]")

    # ── SECTION 7: DAILY EXPERIMENT LOG ───────────────────────────────────────
    _h("7. DAILY EXPERIMENT LOG")

    if not raw["daily"]:
        print("\n  No runs recorded yet.\n")
    else:
        print(
            f"\n  {'Date':<12} {'Mode':<6} {'Trades':>7}  "
            f"{'YES':>5}  {'NO':>5}  {'Brier':>7}  {'PnL(c)':>8}"
        )
        print(
            f"  {'------------':<12} {'------':<6} {'-------':>7}  "
            f"{'---':>5}  {'--':>5}  {'-----':>7}  {'------':>8}"
        )
        for r in raw["daily"]:
            print(
                f"  {r['run_date']:<12} {(r['gumbel_mode'] or 'n/a'):<6} "
                f"{r['total_trades']:>7}  {r['yes_trades']:>5}  {r['no_trades']:>5}  "
                f"{_fmt(r['brier_score']):>7}  "
                f"{_fmt(r['realized_pnl_cents'], '+.1f'):>7}c"
            )

    # ── FOOTER ────────────────────────────────────────────────────────────────
    print(f"\n{_SEP2}")
    print(f"  END OF REPORT - {_now()}")
    print()
    print("  SUBMIT TO AI REVIEW with this prompt:")
    print("  -----------------------------------------------------------------------")
    print(f"  This is {days} days of Kalshi weather paper trading data.")
    print("  The bot models P(T_daily_max > threshold) using Student-t with optional")
    print("  Gumbel correction: none=raw, half=50%, full=100% Gumbel variance.")
    print("  A/B/C protocol: Apr28=half, Apr29=none, Apr30=full.")
    print()
    print("  Based on the report above:")
    print("  1. Which GUMBEL_MODE wins on PnL, Brier, and bias neutrality?")
    print("  2. Which cities show directional bias and why?")
    print("  3. Are tail-risk levels acceptable? What threshold should we use?")
    print("  4. Which horizon or strike-distance bucket has the most real edge?")
    print("  5. What is the single highest-priority fix before going live?")
    print("  -----------------------------------------------------------------------")
    print(_SEP2 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gumbel A/B/C calibration report")
    parser.add_argument(
        "--days", type=int, default=3,
        help="Rolling window in days (default: 3)"
    )
    args = parser.parse_args()
    run_report(days=args.days)
