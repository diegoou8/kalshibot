"""
Gumbel A/B/C experiment schedule — single source of truth.

The bot reads this at startup and applies each mode automatically at
PRUNE_HOUR_UTC (4 AM UTC = midnight US Eastern, after all daily settlements).

To add a new round: append rows below.
To manually override mid-day: python scripts/set_gumbel_mode.py <mode>
"""
from typing import Dict

# UTC date → gumbel_mode
GUMBEL_SCHEDULE: Dict[str, str] = {
    # ── Phase 1 (Apr–May 2026) ────────────────────────────────────────────────
    "2026-04-28": "half",
    "2026-04-29": "none",
    "2026-04-30": "full",
    "2026-05-04": "none",
    "2026-05-05": "none",

    # ── Phase 2 (May–Jun 2026) ────────────────────────────────────────────────
    # 3 days per mode gives enough trades to compare edge/Brier/PnL across modes.
    # After each 9-day block, run: python analytics/calibration_report.py --days 9
    # 2026-05-25: excluded — fill-tracking bug caused 162 contracts over-bought before fix deploy
    "2026-05-25": "half",
    "2026-05-26": "half",
    "2026-05-27": "half",
    "2026-05-28": "none",
    "2026-05-29": "none",
    "2026-05-30": "none",
    "2026-06-01": "full",
    "2026-06-02": "full",
    "2026-06-03": "full",

    # ── Phase 3 (Jun 2026) ────────────────────────────────────────────────────
    "2026-06-04": "half",
    "2026-06-05": "half",
    "2026-06-06": "half",
    "2026-06-07": "none",
    "2026-06-08": "none",
    "2026-06-09": "none",
    "2026-06-10": "full",
    "2026-06-11": "full",
    "2026-06-12": "full",
}
