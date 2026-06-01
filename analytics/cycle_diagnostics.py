"""
Cycle diagnostics report — printed at the end of every trade cycle.

Collects stats during the cycle (via CycleDiagnostics) and generates a
plain-text report with funnel metrics, gate breakdowns, calibration stats,
city/horizon/strike-distance breakdowns, and actionable warnings.

No trading logic is modified. This file is observability-only.
"""
import math
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_SEP  = "─" * 72
_SEP2 = "═" * 72


# ── Per-candidate record ──────────────────────────────────────────────────────

@dataclass
class CandidateRecord:
    ticker: str
    city: Optional[str]
    side: str                  # "yes" | "no"
    p_model: float             # brain's P(YES)
    p_market: float            # market-implied P(YES) = yes_ask / 100
    tau_hrs: float
    strike_z: float            # (threshold − forecast) / sigma  (signed)
    filled: bool = False


# ── Main accumulator ─────────────────────────────────────────────────────────

@dataclass
class CycleDiagnostics:
    cycle_start: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds"))

    # Funnel counts
    n_scanned: int = 0              # markets fetched from Kalshi
    n_spread_ok: int = 0            # passed Phase-1a spread filter
    n_tau_skip: int = 0             # skipped: tau < 6h
    n_no_p_yes: int = 0             # skipped: no weather estimate
    n_edge_fail: int = 0            # skipped: edge < min_edge
    n_engine_none: int = 0          # engine returned None (EV too low after Kelly)
    n_gate_fail: int = 0            # failed any gate
    n_risk_fail: int = 0            # failed preflight_check
    n_candidates: int = 0           # passed all in-loop filters
    n_already_held: int = 0         # dedup: slot already open in DB
    n_dedup_removed: int = 0        # dedup: lower-EV duplicate in same slot
    n_blocked_opposite_side: int = 0      # dedup: same slot but opposite side
    n_blocked_strike_too_close: int = 0   # dedup: strike within 2°F of existing
    n_blocked_contract_cap: int = 0       # dedup: would exceed 4 contracts in slot
    n_fills: int = 0                # orders that actually filled

    # KXTEMP hourly-temp market sub-funnel
    n_kxtemp_scanned:       int = 0   # KXTEMP markets after spread filter
    n_kxtemp_unknown_city:  int = 0   # failed: suffix not in alias map
    n_kxtemp_no_estimate:   int = 0   # parsed OK but estimate_p_yes returned None
    n_kxtemp_p_yes:         int = 0   # produced a valid P(YES)

    # Gate breakdown (key = "EV" | "STALE" | "SPREAD" | "FRAGILITY" |
    #                       "ESS" | "DEPTH" | "SETTLEMENT" | "VARIANCE")
    gate_counts: Counter = field(default_factory=Counter)

    # Candidate records (all that reach the gate step, win or lose)
    candidates: List[CandidateRecord] = field(default_factory=list)

    # ── Recording helpers ─────────────────────────────────────────────────────

    def record_gate_fail(self, reasons: List[str]) -> None:
        self.n_gate_fail += 1
        for r in reasons:
            # reasons look like "EV_FAIL: 2.50 <= 3.0"
            gate_name = r.split("_FAIL")[0] if "_FAIL" in r else r.split(":")[0]
            self.gate_counts[gate_name.strip()] += 1

    def record_candidate(
        self,
        ticker: str,
        city: Optional[str],
        side: str,
        p_model: float,
        p_market: float,
        tau_hrs: float,
        strike_z: float,
    ) -> None:
        self.n_candidates += 1
        self.candidates.append(CandidateRecord(
            ticker=ticker, city=city, side=side,
            p_model=p_model, p_market=p_market,
            tau_hrs=tau_hrs, strike_z=strike_z,
        ))

    def mark_filled(self, ticker: str) -> None:
        for c in self.candidates:
            if c.ticker == ticker:
                c.filled = True
                break

    # ── Report generation ─────────────────────────────────────────────────────

    def generate_report(self, db) -> str:
        lines: List[str] = []

        def h(title: str) -> None:
            lines.append("")
            lines.append(_SEP)
            lines.append(f"  {title}")
            lines.append(_SEP)

        lines.append("")
        lines.append(_SEP2)
        lines.append(f"  CYCLE DIAGNOSTICS — {self.cycle_start} UTC")
        lines.append(_SEP2)

        # ── 1. Funnel ─────────────────────────────────────────────────────────
        h("TRADE FUNNEL")
        n_valid = self.n_spread_ok
        pct = lambda n, d: f"{100*n/d:.0f}%" if d else "n/a"
        lines.append(f"  Markets scanned       : {self.n_scanned:>5}")
        lines.append(f"  After spread filter   : {n_valid:>5}  ({pct(n_valid, self.n_scanned)} pass)")
        lines.append(f"  Tau < 6h skip         : {self.n_tau_skip:>5}")
        lines.append(f"  No P(YES) estimate    : {self.n_no_p_yes:>5}")
        lines.append(f"  Edge filter fail      : {self.n_edge_fail:>5}")
        lines.append(f"  Engine EV fail        : {self.n_engine_none:>5}")
        lines.append(f"  Gate fail             : {self.n_gate_fail:>5}")
        lines.append(f"  Risk/preflight fail   : {self.n_risk_fail:>5}")
        lines.append(f"  Candidates (pre-dedup): {self.n_candidates:>5}")
        lines.append(f"  Already held (skip)   : {self.n_already_held:>5}")
        lines.append(f"  Opp-side blocked      : {self.n_blocked_opposite_side:>5}")
        lines.append(f"  Strike too close      : {self.n_blocked_strike_too_close:>5}")
        lines.append(f"  Contract cap blocked  : {self.n_blocked_contract_cap:>5}")
        lines.append(f"  Dedup removed         : {self.n_dedup_removed:>5}")
        lines.append(f"  Final fills           : {self.n_fills:>5}")

        # ── 1b. KXTEMP coverage sub-funnel ────────────────────────────────────
        if self.n_kxtemp_scanned > 0:
            h("KXTEMP COVERAGE (hourly temp markets)")
            _pct_pyp = pct(self.n_kxtemp_p_yes, self.n_kxtemp_scanned)
            lines.append(f"  Scanned           : {self.n_kxtemp_scanned:>5}")
            lines.append(f"  Produced P(YES)   : {self.n_kxtemp_p_yes:>5}  ({_pct_pyp})")
            lines.append(f"  Unknown city code : {self.n_kxtemp_unknown_city:>5}")
            lines.append(f"  No estimate       : {self.n_kxtemp_no_estimate:>5}")

        # ── 2. Gate breakdown ─────────────────────────────────────────────────
        if self.gate_counts:
            h("GATE REJECTION BREAKDOWN")
            gate_order = ["EV", "STALE", "SPREAD", "FRAGILITY", "ESS", "DEPTH", "SETTLEMENT", "VARIANCE"]
            for g in gate_order:
                if self.gate_counts[g]:
                    lines.append(f"  {g:<14}: {self.gate_counts[g]:>4}")
            others = {k: v for k, v in self.gate_counts.items() if k not in gate_order}
            for k, v in sorted(others.items()):
                lines.append(f"  {k:<14}: {v:>4}")

        if not self.candidates:
            lines.append("")
            lines.append("  No candidates reached the evaluation step this cycle.")
            lines.append(_SEP2)
            return "\n".join(lines)

        # ── 3. YES/NO distribution ────────────────────────────────────────────
        h("YES / NO CANDIDATE DISTRIBUTION")
        n_yes = sum(1 for c in self.candidates if c.side == "yes")
        n_no  = sum(1 for c in self.candidates if c.side == "no")
        total = len(self.candidates)
        lines.append(f"  YES candidates: {n_yes:>4}  ({pct(n_yes, total)})")
        lines.append(f"  NO  candidates: {n_no:>4}  ({pct(n_no,  total)})")

        # ── 4. Overall calibration ────────────────────────────────────────────
        h("CALIBRATION SUMMARY (all candidates)")
        pm_avg  = _avg([c.p_model  for c in self.candidates])
        pmk_avg = _avg([c.p_market for c in self.candidates])
        diff_avg = _avg([c.p_model - c.p_market for c in self.candidates])
        lines.append(f"  Avg P_model           : {pm_avg:+.3f}")
        lines.append(f"  Avg P_market          : {pmk_avg:+.3f}")
        lines.append(f"  Avg P_model−P_market  : {diff_avg:+.3f}  ({diff_avg*100:+.1f}c)")

        # ── 5. City breakdown ─────────────────────────────────────────────────
        h("BY CITY")
        city_groups: Dict[str, List[CandidateRecord]] = defaultdict(list)
        for c in self.candidates:
            city_groups[c.city or "UNK"].append(c)
        lines.append(f"  {'City':<8} {'N':>4}  {'YES':>4}  {'NO':>4}  "
                     f"{'P_model':>8}  {'P_market':>8}  {'Diff':>8}")
        lines.append(f"  {'─'*8} {'─'*4}  {'─'*4}  {'─'*4}  "
                     f"{'─'*8}  {'─'*8}  {'─'*8}")
        for city in sorted(city_groups):
            grp = city_groups[city]
            ny  = sum(1 for x in grp if x.side == "yes")
            nn  = sum(1 for x in grp if x.side == "no")
            pm  = _avg([x.p_model  for x in grp])
            pmk = _avg([x.p_market for x in grp])
            d   = _avg([x.p_model - x.p_market for x in grp])
            lines.append(f"  {city:<8} {len(grp):>4}  {ny:>4}  {nn:>4}  "
                         f"{pm:>8.3f}  {pmk:>8.3f}  {d:>+8.3f}")

        # ── 6. Horizon bucket breakdown ───────────────────────────────────────
        h("BY HORIZON BUCKET")
        tau_groups: Dict[str, List[CandidateRecord]] = defaultdict(list)
        for c in self.candidates:
            tau_groups[_tau_bin(c.tau_hrs)].append(c)
        _tau_order = ["<6h", "6-12h", "12-24h", "24-48h", "48h+"]
        lines.append(f"  {'Horizon':<8} {'N':>4}  {'P_model':>8}  {'P_market':>8}  {'Diff':>8}")
        lines.append(f"  {'─'*8} {'─'*4}  {'─'*8}  {'─'*8}  {'─'*8}")
        for bucket in _tau_order:
            grp = tau_groups.get(bucket)
            if not grp:
                continue
            pm  = _avg([x.p_model  for x in grp])
            pmk = _avg([x.p_market for x in grp])
            d   = _avg([x.p_model - x.p_market for x in grp])
            lines.append(f"  {bucket:<8} {len(grp):>4}  {pm:>8.3f}  {pmk:>8.3f}  {d:>+8.3f}")

        # ── 7. Strike distance breakdown ──────────────────────────────────────
        h("BY STRIKE DISTANCE (|threshold − forecast| / sigma)")
        z_groups: Dict[str, List[CandidateRecord]] = defaultdict(list)
        for c in self.candidates:
            z_groups[_z_bin(c.strike_z)].append(c)
        _z_order = ["<1σ", "1-2σ", "2-3σ", ">3σ", "n/a"]
        lines.append(f"  {'Z-bucket':<8} {'N':>4}  {'YES':>4}  {'NO':>4}  {'P_model':>8}  {'Diff':>8}")
        lines.append(f"  {'─'*8} {'─'*4}  {'─'*4}  {'─'*4}  {'─'*8}  {'─'*8}")
        for bucket in _z_order:
            grp = z_groups.get(bucket)
            if not grp:
                continue
            ny  = sum(1 for x in grp if x.side == "yes")
            nn  = sum(1 for x in grp if x.side == "no")
            pm  = _avg([x.p_model  for x in grp])
            d   = _avg([x.p_model - x.p_market for x in grp])
            lines.append(f"  {bucket:<8} {len(grp):>4}  {ny:>4}  {nn:>4}  {pm:>8.3f}  {d:>+8.3f}")

        # ── 8. Warnings ───────────────────────────────────────────────────────
        warnings: List[str] = []

        # 8a. One-sided candidate distribution
        if total >= 5:
            pct_yes = n_yes / total
            pct_no  = n_no  / total
            if pct_yes > 0.80:
                warnings.append(
                    f"⚠ {pct_yes*100:.0f}% of candidates are YES — model may be systematically long."
                )
            if pct_no > 0.80:
                warnings.append(
                    f"⚠ {pct_no*100:.0f}% of candidates are NO — model may be systematically short "
                    f"(currently {pct_no*100:.0f}%; check σ calibration)."
                )

        # 8b. City Brier > 0.25 over last 30 settled predictions
        city_brier = _get_city_brier(db, n=30)
        for city, (brier, n_settled) in sorted(city_brier.items()):
            if n_settled >= 10 and brier > 0.25:
                warnings.append(
                    f"⚠ {city} Brier={brier:.3f} over last {n_settled} settled predictions "
                    f"(target < 0.25). Consider pausing {city} trades."
                )

        # 8c. Consistent P_model − P_market sign by city
        for city in sorted(city_groups):
            grp = city_groups[city]
            if len(grp) < 3:
                continue
            diffs = [c.p_model - c.p_market for c in grp]
            mean_diff = sum(diffs) / len(diffs)
            if abs(mean_diff) > 0.10:
                direction = "above" if mean_diff > 0 else "below"
                warnings.append(
                    f"⚠ {city}: P_model is consistently {mean_diff*100:+.1f}c {direction} "
                    f"P_market ({len(grp)} candidates). Possible σ miscalibration."
                )

        if warnings:
            h("WARNINGS")
            for w in warnings:
                lines.append(f"  {w}")

        lines.append("")
        lines.append(_SEP2)
        return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _avg(vals: List[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _tau_bin(tau: float) -> str:
    if tau < 6:   return "<6h"
    if tau < 12:  return "6-12h"
    if tau < 24:  return "12-24h"
    if tau < 48:  return "24-48h"
    return "48h+"


def _z_bin(z: float) -> str:
    if math.isnan(z) or math.isinf(z):
        return "n/a"
    az = abs(z)
    if az < 1:  return "<1σ"
    if az < 2:  return "1-2σ"
    if az < 3:  return "2-3σ"
    return ">3σ"


def _get_city_brier(db, n: int = 30) -> Dict[str, tuple]:
    """Return {city: (avg_brier, count)} for the last n settled predictions per city."""
    try:
        with db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT city, AVG(brier_score) AS brier, COUNT(*) AS n
                FROM (
                    SELECT city, brier_score,
                           ROW_NUMBER() OVER (PARTITION BY city ORDER BY recorded_at DESC) AS rn
                    FROM predictions
                    WHERE actual_outcome IS NOT NULL
                      AND brier_score    IS NOT NULL
                      AND city           IS NOT NULL
                )
                WHERE rn <= ?
                GROUP BY city
                """,
                (n,),
            ).fetchall()
        return {r["city"]: (r["brier"], r["n"]) for r in rows}
    except Exception as exc:
        logger.debug("City Brier query failed: %s", exc)
        return {}


def compute_strike_z(ticker: str, sigma: float) -> float:
    """
    Signed z-score: (threshold − forecast) / sigma.
    Positive = forecast is below threshold (NO bet zone).
    Returns NaN if forecast is not cached or ticker unparseable.
    """
    try:
        from src.brain.weather_estimator import _parse_ticker, get_forecast_temp_for_ticker
        parsed = _parse_ticker(ticker)
        if not parsed:
            return float("nan")
        forecast = get_forecast_temp_for_ticker(ticker)
        if forecast is None:
            return float("nan")
        mtype = parsed["type"]
        if mtype == "HIGH_BAND":
            threshold = parsed["lower"] + 0.5
        elif mtype == "HIGH_ABOVE":
            threshold = parsed["threshold"]
        elif mtype == "HOURLY_ABOVE":
            threshold = parsed["threshold"]
        else:
            return float("nan")
        return (threshold - forecast) / sigma if sigma > 0 else float("nan")
    except Exception:
        return float("nan")
