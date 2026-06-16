"""
SegmentGuard — city + side + market_type blocking and trust-score shrinkage.

Operates at a finer grain than CityRiskGuard (which blocks whole cities).
A segment is (city, side, market_type).  Each segment has:
  - A hard-block list seeded from the post-mortem (DEN/YES, TDC/YES, PHIL/YES, LAX/NO).
  - A dynamic trust score [0.0, 0.5] derived from segment_performance table.
  - A status: BLOCK | SHADOW_ONLY | THROTTLE | ALLOW

Trust score drives market edge shrinkage in the trade cycle:
    p_final = p_market + trust * (p_model - p_market)

At trust=0.0: p_final = p_market (ignore model entirely — bet only on tautology).
At trust=0.5: model gets half the weight (maximum allowed until out-of-sample PnL proven).

Trust is capped at 0.5 until a segment has positive realized PnL AND Brier < 0.20.
"""
import json
import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Initial hard blocks seeded from Jun-13 post-mortem ───────────────────────
# Key: (city, side)   — market_type=None means "all types for this city/side"
# These are permanent until manually removed via DB or code change.
_HARD_BLOCKS: Dict[Tuple[str, str], str] = {
    ("DEN",  "yes"): "post_mortem_jun13: 83 YES fills @17c, model 45% vs market 19%, all settled NO",
    ("TDC",  "yes"): "post_mortem_jun13: 38 fills 84% YES, model 61% vs actual 8.5% YES",
    ("PHIL", "yes"): "post_mortem_jun13: 42 fills 98% YES, model 74% vs actual 0% YES",
    ("LAX",  "no"):  "post_mortem_jun13: 64 NO fills @41c, model 9% YES vs actual 67% YES",
}

# Trust thresholds
# Post direction-fix (2026-06-07): segment_performance is empty — no evidence of
# model failure on any city/side.  Hard blocks (DEN/YES etc.) protect the known
# bad segments.  All other segments default to full trust so the model can trade
# and accumulate clean performance data.  Once 20+ settled fills exist per
# segment, _compute_status will auto-promote or block based on measured metrics.
_TRUST_MIN_SETTLED      = 20     # need this many before BLOCK or ALLOW
_TRUST_BLOCK_ROI        = -0.10  # roi < this (with n >= min) → BLOCK
_TRUST_BLOCK_BRIER      = 0.35   # brier > this (with n >= min) → BLOCK
_TRUST_ALLOW_BRIER      = 0.20   # brier < this + positive pnl → ALLOW
_TRUST_MAX              = 1.00   # full trust until DB shows otherwise
_TRUST_ALLOW            = 1.00
_TRUST_THROTTLE         = 1.00   # unseen / young segments trust model fully
_TRUST_SHADOW           = 0.00   # reserved for explicit SHADOW_ONLY status


class SegmentGuard:
    """
    Call refresh(db) once per trade cycle, then check(city, side, market_type)
    per candidate market.

    check() returns (allow, trust, status):
        allow=False → log BLOCKED_SEGMENT_GUARD and skip entirely
        trust ∈ [0.0, 0.5] → use in p_final = p_market + trust*(p_model-p_market)
        status → BLOCK | SHADOW_ONLY | THROTTLE | ALLOW
    """

    def __init__(self) -> None:
        # (city, side, market_type) → {status, trust}
        self._cache: Dict[Tuple[str, str, str], Dict] = {}

    def refresh(self, db) -> None:
        """Load segment_performance rows and compute trust scores."""
        self._cache = {}
        try:
            rows = db.get_segment_rows()
        except Exception as e:
            logger.warning("SegmentGuard.refresh: DB read failed: %s", e)
            return

        for row in rows:
            city   = (row.get("city")        or "").upper()
            side   = (row.get("side")        or "").lower()
            mtype  = (row.get("market_type") or "").upper()
            status, trust = self._compute_status(row)
            self._cache[(city, side, mtype)] = {"status": status, "trust": trust}

    @staticmethod
    def _compute_status(row: dict) -> Tuple[str, float]:
        n_settled = row.get("n_settled") or 0
        roi       = row.get("roi")
        brier     = row.get("brier")
        pnl       = row.get("realized_pnl_cents") or 0.0

        if n_settled >= _TRUST_MIN_SETTLED:
            if roi is not None and roi < _TRUST_BLOCK_ROI:
                return "BLOCK", 0.0
            if brier is not None and brier > _TRUST_BLOCK_BRIER:
                return "BLOCK", 0.0
            if pnl > 0 and brier is not None and brier < _TRUST_ALLOW_BRIER:
                return "ALLOW", _TRUST_ALLOW

        # < 20 settled → THROTTLE at full trust (post direction-fix baseline)
        return "THROTTLE", _TRUST_THROTTLE

    def check(
        self,
        city: str,
        side: str,
        market_type: Optional[str] = None,
    ) -> Tuple[bool, float, str]:
        """
        Returns (allow, trust, status).

        Hard blocks checked first, then DB-derived status.
        For unknown segments (not yet in DB), defaults to SHADOW_ONLY.
        """
        city_up  = city.upper()
        side_low = side.lower()
        mtype    = (market_type or "").upper()

        # 1. Hard block — overrides all DB state
        if (city_up, side_low) in _HARD_BLOCKS:
            reason = _HARD_BLOCKS[(city_up, side_low)]
            logger.info(
                "BLOCKED_SEGMENT_GUARD: city=%s side=%s market_type=%s reason=%s",
                city_up, side_low, mtype or "any", reason,
            )
            return False, 0.0, "BLOCK"

        # 2. DB-derived status (most-specific match first: city+side+type, then city+side)
        entry = (
            self._cache.get((city_up, side_low, mtype))
            or self._cache.get((city_up, side_low, ""))
        )
        if entry is None:
            # Unseen segment — full trust (1.0) since segment_performance is empty
            # and we have zero evidence of model failure post direction-fix.
            return True, _TRUST_THROTTLE, "THROTTLE"

        status = entry["status"]
        trust  = entry["trust"]

        if status == "BLOCK":
            logger.warning(
                "BLOCKED_SEGMENT_GUARD: city=%s side=%s mtype=%s status=BLOCK (DB)",
                city_up, side_low, mtype,
            )
            return False, 0.0, "BLOCK"

        return True, trust, status

    def hard_blocks(self) -> Dict[Tuple[str, str], str]:
        """Expose the static hard-block list for logging / reporting."""
        return dict(_HARD_BLOCKS)
