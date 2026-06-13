"""
Settlement source audit: compares Open-Meteo historical temperature to
Kalshi's actual settlement result for every settled fill.

Goal: quantify how often our Open-Meteo forecast model would have gotten the
RIGHT answer vs what Kalshi actually settled, broken down by city and market type.
This exposes any systematic NWS vs Open-Meteo temperature offset.

Usage:
    python scripts/settlement_source_audit.py            # all settled fills
    python scripts/settlement_source_audit.py --city DEN # focus on one city
    python scripts/settlement_source_audit.py --days 30  # last 30 days only
"""
import asyncio
import argparse
import sys
import re
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Optional, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from src.services.kalshi_client import client
from src.db.dwtrader import DWTraderDB
from src.brain.weather_estimator import _parse_ticker, _CITY_MAP
import aiohttp


# ── Open-Meteo archive fetch ─────────────────────────────────────────────────

async def _fetch_archive_daily_max(lat: float, lon: float, tz: str, target_date: str) -> Optional[float]:
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": tz,
        "start_date": target_date,
        "end_date": target_date,
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as sess:
        async with sess.get("https://archive-api.open-meteo.com/v1/archive", params=params) as r:
            if r.status != 200:
                return None
            data = await r.json()
            temps = data.get("daily", {}).get("temperature_2m_max", [])
            return float(temps[0]) if temps else None


async def _fetch_archive_hourly(lat: float, lon: float, tz: str, target_date: str, hour: int) -> Optional[float]:
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": tz,
        "start_date": target_date,
        "end_date": target_date,
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as sess:
        async with sess.get("https://archive-api.open-meteo.com/v1/archive", params=params) as r:
            if r.status != 200:
                return None
            data = await r.json()
            temps = data.get("hourly", {}).get("temperature_2m", [])
            return float(temps[hour]) if len(temps) > hour else None


# ── Data fetching ─────────────────────────────────────────────────────────────

def _get_settled_fills(db: DWTraderDB, city_filter: Optional[str], days: Optional[int]) -> List[Dict]:
    with db.get_connection() as conn:
        c = conn.cursor()
        where_parts = ["ta.realized_pnl_cents IS NOT NULL"]
        params = []
        if city_filter:
            where_parts.append("ta.city = ?")
            params.append(city_filter.upper())
        if days:
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            where_parts.append("CAST(e.timestamp AS DATE) >= ?")
            params.append(cutoff)
        where = " AND ".join(where_parts)
        c.execute(
            f"""
            SELECT DISTINCT
                ta.ticker, ta.city, ta.side,
                ta.fill_price_cents, ta.realized_pnl_cents,
                o.status AS order_status
            FROM trade_attribution ta
            JOIN executions e ON e.attribution_id = ta.attribution_id
            LEFT JOIN orders o ON o.ticker = ta.ticker
            WHERE {where}
            ORDER BY ta.ticker
            """,
            params,
        )
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, row)) for row in c.fetchall()]


# ── Audit one ticker ──────────────────────────────────────────────────────────

async def _audit_ticker_settlement(
    ticker: str, city: str, side: str, fill_price: float, realized_pnl: float
) -> Dict:
    parsed = _parse_ticker(ticker.upper())
    if not parsed:
        return {"ticker": ticker, "error": "unparseable"}

    city_info = _CITY_MAP.get(city)
    if not city_info:
        return {"ticker": ticker, "error": f"unknown city {city}"}

    lat, lon, tz = city_info[0], city_info[1], city_info[2]
    settle_date = None
    m = re.match(r"KX(?:HIGH|TEMP)[A-Z]+-(\d{2}[A-Z]{3}\d{2})", ticker, re.IGNORECASE)
    if m:
        try:
            settle_date = datetime.strptime(m.group(1).upper(), "%y%b%d").strftime("%Y-%m-%d")
        except ValueError:
            pass

    if not settle_date:
        return {"ticker": ticker, "error": "date parse failed"}

    mtype = parsed.get("type")
    om_temp: Optional[float] = None
    if mtype == "HIGH_BAND":
        om_temp = await _fetch_archive_daily_max(lat, lon, tz, settle_date)
        lower, upper = parsed["lower"], parsed["upper"]
        om_yes = (om_temp is not None and lower <= om_temp < upper)
    elif mtype == "HIGH_ABOVE":
        om_temp = await _fetch_archive_daily_max(lat, lon, tz, settle_date)
        thresh = parsed["threshold"]
        om_yes = (om_temp is not None and om_temp > thresh)
    elif mtype == "HOURLY_ABOVE":
        om_temp = await _fetch_archive_hourly(lat, lon, tz, settle_date, parsed["hour"])
        thresh = parsed["threshold"]
        om_yes = (om_temp is not None and om_temp >= thresh)
    else:
        return {"ticker": ticker, "error": f"unknown type {mtype}"}

    # Kalshi actual: infer from realized PnL
    # won if PnL > 0 (side matched outcome), lost if PnL < 0
    we_won = realized_pnl > 0
    # YES outcome = we won and side=yes, or we lost and side=no
    kalshi_yes = (we_won and side == "yes") or (not we_won and side == "no")

    agrees = (om_yes == kalshi_yes) if om_temp is not None else None
    delta = (om_temp - (parsed.get("lower", parsed.get("threshold", 0)))) if om_temp else None

    return {
        "ticker":       ticker,
        "city":         city,
        "market_type":  mtype,
        "settle_date":  settle_date,
        "fill_price":   fill_price,
        "realized_pnl": realized_pnl,
        "side":         side,
        "om_temp_f":    om_temp,
        "om_says_yes":  om_yes if om_temp is not None else None,
        "kalshi_yes":   kalshi_yes,
        "agrees":       agrees,
        "delta_f":      delta,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(city_filter: Optional[str], days: Optional[int]) -> None:
    db = DWTraderDB()
    fills = _get_settled_fills(db, city_filter, days)
    if not fills:
        print("No settled fills found.")
        return

    print(f"\nSETTLEMENT SOURCE AUDIT — {len(fills)} settled fills")
    print("=" * 80)

    sem = asyncio.Semaphore(6)
    async def bounded(f):
        async with sem:
            return await _audit_ticker_settlement(
                f["ticker"], f["city"], f["side"],
                f["fill_price_cents"], f["realized_pnl_cents"]
            )

    rows = await asyncio.gather(*[bounded(f) for f in fills])

    # Print results
    print(f"\n{'TICKER':<38} {'TYPE':<14} {'OM_TEMP':>8} {'OM_YES':>7} {'KAL_YES':>8} {'AGREE':>7}")
    print("-" * 80)
    agree_count = disagree_count = unknown_count = 0
    city_stats: Dict[str, Dict] = {}

    for r in sorted(rows, key=lambda x: x.get("ticker", "")):
        if "error" in r:
            print(f"  {r['ticker']:<38} ERROR: {r['error']}")
            continue
        ag = r.get("agrees")
        city = r.get("city", "?")
        if city not in city_stats:
            city_stats[city] = {"agree": 0, "disagree": 0, "unknown": 0, "delta_sum": 0.0, "n_delta": 0}
        if ag is True:
            agree_count += 1
            city_stats[city]["agree"] += 1
        elif ag is False:
            disagree_count += 1
            city_stats[city]["disagree"] += 1
            print(
                f"  {r['ticker']:<38} {r['market_type']:<14} "
                f"{r['om_temp_f']:>7.1f}° {str(r['om_says_yes']):>7} "
                f"{str(r['kalshi_yes']):>8} {'NO':>7}  ← DISAGREE"
            )
        else:
            unknown_count += 1
            city_stats[city]["unknown"] += 1

        if r.get("delta_f") is not None:
            city_stats[city]["delta_sum"] += r["delta_f"]
            city_stats[city]["n_delta"] += 1

    total_known = agree_count + disagree_count
    agree_rate = agree_count / total_known if total_known else 0.0

    print("\n" + "=" * 80)
    print(f"OVERALL: {agree_count}/{total_known} agree ({agree_rate:.1%}) — {disagree_count} discrepancies, {unknown_count} unknown")

    print(f"\n{'CITY':<8} {'AGREE':>7} {'DISAGREE':>9} {'UNKNOWN':>8} {'AGREE%':>8}")
    print("-" * 45)
    for city, s in sorted(city_stats.items()):
        tot = s["agree"] + s["disagree"]
        rate = s["agree"] / tot if tot else 0.0
        flag = "  ← HIGH DISCREPANCY" if s["disagree"] > 2 else ""
        print(f"  {city:<6} {s['agree']:>7} {s['disagree']:>9} {s['unknown']:>8} {rate:>8.1%}{flag}")

    if disagree_count > 0:
        print(
            f"\nWARNING: {disagree_count} disagreements mean Open-Meteo and NWS gave different"
            f" outcomes for the same market -- confirms MODEL_SOURCE_MISMATCH_RISK."
        )
        print("   Calibrate per-city temperature offsets in weather_estimator.py::_AR1_MU.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare Open-Meteo archive to Kalshi settlement.")
    parser.add_argument("--city", default=None, help="Filter to one city code (e.g. DEN)")
    parser.add_argument("--days", type=int, default=None, help="Only look back N days")
    args = parser.parse_args()
    asyncio.run(main(args.city, args.days))
