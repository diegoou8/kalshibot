"""
Override GUMBEL_MODE in the bot_config DB table for the A/B/C experiment.

Works both locally (reads .env) and inside the Azure container (via SSH).
The running bot picks up the new value on the next trade cycle — no restart needed.

Usage:
    python scripts/set_gumbel_mode.py none
    python scripts/set_gumbel_mode.py half
    python scripts/set_gumbel_mode.py full
    python scripts/set_gumbel_mode.py        # show current value + schedule

The bot auto-applies the schedule daily — this script is for manual overrides only.
To add a new experiment date, edit GUMBEL_SCHEDULE in bot_runner.py.
"""
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

# Load .env locally (no-op if AZURE_SQL_CONN_STR is already in the environment)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config.experiment import GUMBEL_SCHEDULE  # single source of truth

_VALID_MODES = ("none", "half", "full")


def _connect():
    import pyodbc
    conn_str = os.environ.get("AZURE_SQL_CONN_STR")
    if not conn_str:
        print("[error] AZURE_SQL_CONN_STR is not set — add it to .env or the environment.")
        sys.exit(1)
    return pyodbc.connect(conn_str, autocommit=False)


def _get_current(conn) -> str:
    c = conn.cursor()
    c.execute("SELECT value FROM bot_config WHERE config_key = 'GUMBEL_MODE'")
    row = c.fetchone()
    return row[0] if row else os.getenv("GUMBEL_MODE", "half") + " (env fallback)"


def _set(conn, mode: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    c = conn.cursor()
    c.execute(
        """
        MERGE bot_config AS tgt
        USING (SELECT ? AS config_key, ? AS value, ? AS updated_at) AS src
        ON tgt.config_key = src.config_key
        WHEN MATCHED THEN
            UPDATE SET value = src.value, updated_at = src.updated_at
        WHEN NOT MATCHED THEN
            INSERT (config_key, value, updated_at)
            VALUES (src.config_key, src.value, src.updated_at);
        """,
        ("GUMBEL_MODE", mode, now),
    )
    conn.commit()


def _next_scheduled(today: str):
    dates = sorted(d for d in GUMBEL_SCHEDULE if d > today)
    if dates:
        return dates[0], GUMBEL_SCHEDULE[dates[0]]
    return None, None


def show_status() -> None:
    conn = _connect()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    current = _get_current(conn)
    conn.close()

    scheduled_today = GUMBEL_SCHEDULE.get(today, "not scheduled")
    next_date, next_mode = _next_scheduled(today)

    print(f"Current GUMBEL_MODE (bot_config): {current}")
    print(f"Today ({today}) schedule:          {scheduled_today}")
    if next_date:
        print(f"Next scheduled change ({next_date}): {next_mode}")
    print()
    print("Full A/B/C schedule (auto-applied by bot at midnight UTC):")
    for d, m in sorted(GUMBEL_SCHEDULE.items()):
        marker = " <-- today" if d == today else ""
        print(f"  {d}: {m}{marker}")
    print()
    print("After each day's trading:")
    print("  python analytics/calibration_report.py --days 3")


def set_mode(mode: str) -> None:
    if mode not in _VALID_MODES:
        print(f"[error] Invalid mode '{mode}'. Choose from: {', '.join(_VALID_MODES)}")
        sys.exit(1)

    conn = _connect()
    _set(conn, mode)
    conn.close()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    scheduled_today = GUMBEL_SCHEDULE.get(today, "not scheduled")
    next_date, next_mode = _next_scheduled(today)

    print(f"[ok] GUMBEL_MODE={mode} written to bot_config")
    print(f"     Bot picks it up on next trade cycle (within 5 min), no restart needed.")
    if scheduled_today not in ("not scheduled", mode):
        print(f"[!]  Today ({today}) auto-schedule expects '{scheduled_today}' — you overrode it.")
    if next_date:
        print(f"     Next auto-change: {next_date} -> {next_mode} (happens automatically)")
    print(f"     After trading: python analytics/calibration_report.py --days 3")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        show_status()
        sys.exit(0)
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    set_mode(sys.argv[1].strip().lower())
