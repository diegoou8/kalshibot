"""
Set GUMBEL_MODE in .env for the A/B/C experiment.

Usage:
    python scripts/set_gumbel_mode.py none
    python scripts/set_gumbel_mode.py half
    python scripts/set_gumbel_mode.py full

A/B/C schedule:
    Apr 28: half   (baseline)
    Apr 29: none   (control - no correction)
    Apr 30: full   (maximum correction)
    May 04: none   (extended none sample)
    May 05: none   (extended none sample)
"""
import sys
import re
from pathlib import Path

_VALID_MODES = ("none", "half", "full")
_SCHEDULE = {
    "2026-04-28": "half",
    "2026-04-29": "none",
    "2026-04-30": "full",
    "2026-05-04": "none",
    "2026-05-05": "none",
}
_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _next_day(today: str) -> str:
    dates = sorted(_SCHEDULE)
    for i, d in enumerate(dates):
        if d == today and i + 1 < len(dates):
            return dates[i + 1]
    return "n/a"


def set_mode(mode: str) -> None:
    if mode not in _VALID_MODES:
        print(f"[error] Invalid mode '{mode}'. Choose from: {', '.join(_VALID_MODES)}")
        sys.exit(1)

    # Read existing .env (create if missing)
    if _ENV_PATH.exists():
        content = _ENV_PATH.read_text(encoding="utf-8")
    else:
        content = ""

    # Replace or append GUMBEL_MODE line
    pattern = re.compile(r"^GUMBEL_MODE\s*=.*$", re.MULTILINE)
    new_line = f"GUMBEL_MODE={mode}"
    if pattern.search(content):
        content = pattern.sub(new_line, content)
    else:
        content = content.rstrip("\n") + f"\n{new_line}\n"

    _ENV_PATH.write_text(content, encoding="utf-8")

    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    expected = _SCHEDULE.get(today, "not scheduled")
    next_d   = _next_day(today)
    next_m   = _SCHEDULE.get(next_d, "n/a")

    print(f"[ok] GUMBEL_MODE={mode} written to {_ENV_PATH}")
    if expected != mode:
        print(f"[!]  Today ({today}) schedule expects '{expected}' but you set '{mode}'.")
    else:
        print(f"     Today ({today}) schedule: {mode}  [on schedule]")
    if next_d != "n/a":
        print(f"     Tomorrow ({next_d}): run  python scripts/set_gumbel_mode.py {next_m}")
    print(f"     After trading: python analytics/calibration_report.py --days 3")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        print(f"\nA/B/C schedule:")
        for d, m in sorted(_SCHEDULE.items()):
            print(f"  {d}: {m}")
        sys.exit(1)
    set_mode(sys.argv[1].strip().lower())
