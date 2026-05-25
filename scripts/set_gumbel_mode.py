"""
Set GUMBEL_MODE for the A/B/C experiment by writing to the bot_config DB table.

Works both locally and inside the Azure container (via SSH or az webapp exec).
The running bot reads the new value on the next trade cycle — no restart needed.

Usage:
    python scripts/set_gumbel_mode.py none
    python scripts/set_gumbel_mode.py half
    python scripts/set_gumbel_mode.py full
    python scripts/set_gumbel_mode.py        # show current value + schedule

A/B/C schedule:
    Apr 28: half   (baseline)
    Apr 29: none   (control - no correction)
    Apr 30: full   (maximum correction)
    May 04: none   (extended none sample)
    May 05: none   (extended none sample)
"""
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

# Load .env locally (no-op if env var already set in container)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

_VALID_MODES = ("none", "half", "full")
_SCHEDULE = {
    "2026-04-28": "half",
    "2026-04-29": "none",
    "2026-04-30": "full",
    "2026-05-04": "none",
    "2026-05-05": "none",
}


def _next_scheduled(today: str):
    dates = sorted(d for d in _SCHEDULE if d > today)
    if dates:
        return dates[0], _SCHEDULE[dates[0]]
    return None, None


def _get_db():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.db.dwtrader import DWTraderDB
    return DWTraderDB()


def show_status(db) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    current = db.get_config("GUMBEL_MODE") or os.getenv("GUMBEL_MODE", "half")
    scheduled_today = _SCHEDULE.get(today, "not scheduled")
    next_date, next_mode = _next_scheduled(today)

    print(f"Current GUMBEL_MODE (bot_config): {current}")
    print(f"Today ({today}) schedule:          {scheduled_today}")
    if next_date:
        print(f"Next scheduled change ({next_date}): {next_mode}")
    print()
    print("Full A/B/C schedule:")
    for d, m in sorted(_SCHEDULE.items()):
        marker = " <-- today" if d == today else ""
        print(f"  {d}: {m}{marker}")
    print()
    print("After each day's trading, run:")
    print("  python analytics/calibration_report.py --days 3")


def set_mode(mode: str) -> None:
    if mode not in _VALID_MODES:
        print(f"[error] Invalid mode '{mode}'. Choose from: {', '.join(_VALID_MODES)}")
        sys.exit(1)

    db = _get_db()
    db.set_config("GUMBEL_MODE", mode)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    scheduled_today = _SCHEDULE.get(today, "not scheduled")
    next_date, next_mode = _next_scheduled(today)

    print(f"[ok] GUMBEL_MODE={mode} written to bot_config (takes effect next trade cycle)")
    if scheduled_today != "not scheduled" and scheduled_today != mode:
        print(f"[!]  Today ({today}) schedule expects '{scheduled_today}' but you set '{mode}'.")
    elif scheduled_today == mode:
        print(f"     Today ({today}) schedule: {mode}  [on schedule]")
    if next_date:
        print(f"     Next change ({next_date}): python scripts/set_gumbel_mode.py {next_mode}")
    print(f"     After trading: python analytics/calibration_report.py --days 3")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        db = _get_db()
        show_status(db)
        sys.exit(0)

    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    set_mode(sys.argv[1].strip().lower())
