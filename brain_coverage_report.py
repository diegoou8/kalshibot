#!/usr/bin/env python3
"""
Diagnostic: why does estimate_p_yes() return None for most cities?

Traces each city in _CITY_MAP through every step of the probability pipeline:
  1. Ticker parse    — does _parse_ticker recognise KXHIGH<CITY>-... ?
  2. City map hit    — is the parsed city code in _CITY_MAP?
  3. Forecast fetch  — does Open-Meteo return a temperature for tomorrow?
  4. estimate_p_yes  — does the full call return a value or None?

Also audits per-city DB state:
  - sigma_days : rows in ar1_residuals for this city
  - sigma_val  : MLE sigma (None = fewer than 14 days, uses 4.0deg default)
  - brier      : rolling 30-trade Brier score (None = fewer than 10 trades)

And documents market-type gaps (KXTEMP / KXRAIN).

Usage:
  python brain_coverage_report.py           # full report (needs AZURE_SQL_CONN_STR)
  python brain_coverage_report.py --no-db   # weather-only (no DB required)
"""

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# Load .env so AZURE_SQL_CONN_STR is available when running locally
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass

from src.brain.weather_estimator import (
    _CITY_MAP,
    _parse_ticker,
    _parse_date,
    _fetch_daily_max,
    estimate_p_yes,
    _FORECAST_SIGMA_F,
    _AR1_PHI,
)

# ── Known KXTEMP city-code aliases seen in live Kalshi market scans ──────────
# Key   = city code Kalshi embeds in the ticker (what we parse)
# Value = the key that exists in _CITY_MAP (what would match)
_KXTEMP_ALIASES_SEEN = {
    "NYCH": "NYC",   # from 2026-06-01 log: KXTEMPNYCH-26JUN0111-T61.99
}

# ── Prefixes that _parse_ticker has no regex for ─────────────────────────────
_UNPARSED_PREFIXES = ["KXRAIN"]


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _kxhigh_ticker(city: str, target_date: str) -> str:
    """KXHIGH{CITY}-{YYMMMDD}-B70.5 — a representative band ticker."""
    dt = date.fromisoformat(target_date)
    return f"KXHIGH{city}-{dt.strftime('%y%b%d').upper()}-B70.5"


def _kxtemp_ticker(city_code: str, target_date: str) -> str:
    """KXTEMP{CODE}-{YYMMMDD}11-T65.99 — representative hourly ticker."""
    dt = date.fromisoformat(target_date)
    return f"KXTEMP{city_code}-{dt.strftime('%y%b%d').upper()}11-T65.99"


async def _probe_city(
    city: str,
    lat: float,
    lon: float,
    tz: str,
    target_date: str,
    db,
) -> dict:
    ticker = _kxhigh_ticker(city, target_date)

    # Step 1 — ticker parse
    parsed = _parse_ticker(ticker)
    parse_ok = parsed is not None
    parsed_city = parsed["city"] if parsed else None

    # Step 2 — city map hit
    city_map_ok = parsed_city in _CITY_MAP if parsed_city else False

    # Step 3 — date parse (will always succeed for our constructed ticker)
    iso_date = _parse_date(parsed["date_str"]) if parsed else None
    date_ok = iso_date is not None

    # Step 4 — Open-Meteo forecast fetch (direct, bypasses AR(1) layer)
    forecast_f: Optional[float] = None
    weather_ok = False
    if city_map_ok and date_ok:
        try:
            forecast_f = await _fetch_daily_max(lat, lon, tz, iso_date)
            weather_ok = forecast_f is not None
        except Exception:
            pass

    # Step 5 — full estimate_p_yes call
    p_yes: Optional[float] = await estimate_p_yes(
        ticker, sigma_f=_FORECAST_SIGMA_F, phi=_AR1_PHI, tau_hrs=24.0
    )
    estimate_ok = p_yes is not None

    # DB state
    sigma_days: Optional[int] = None
    sigma_val:  Optional[float] = None
    phi_val:    Optional[float] = None
    brier_val:  Optional[float] = None
    n_brier:    int = 0

    if db is not None:
        try:
            sigma_val = db.get_sigma_mle(city, min_days=14)
            phi_val   = db.get_ar1_phi_estimate(city, min_days=14)
            with db.get_connection() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM ar1_residuals WHERE city = ?", (city,)
                ).fetchone()
                sigma_days = int(row[0]) if row else 0
            brier_val, n_brier = db.get_rolling_brier_by_city(city, window=30, min_obs=10)
        except Exception:
            pass

    # Root cause — earliest step that returned None
    if not parse_ok:
        reason = "TICKER_PARSE_FAIL"
    elif not city_map_ok:
        reason = f"CITY_NOT_IN_MAP (parsed='{parsed_city}')"
    elif not date_ok:
        reason = "DATE_PARSE_FAIL"
    elif not weather_ok:
        reason = "FORECAST_FETCH_FAIL  <- Open-Meteo returned None"
    elif not estimate_ok:
        reason = "UNKNOWN (all inputs OK but estimate_p_yes still None)"
    else:
        reason = "OK"

    return {
        "city":        city,
        "ticker":      ticker,
        "parse_ok":    parse_ok,
        "city_map_ok": city_map_ok,
        "weather_ok":  weather_ok,
        "forecast_f":  forecast_f,
        "sigma_days":  sigma_days,
        "sigma_val":   sigma_val,
        "phi_val":     phi_val,
        "brier_val":   brier_val,
        "n_brier":     n_brier,
        "p_yes":       p_yes,
        "reason":      reason,
    }


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="Brain coverage diagnostic")
    parser.add_argument("--no-db", action="store_true", help="Skip DB queries")
    args = parser.parse_args()

    db = None
    if not args.no_db:
        try:
            from src.db.dwtrader import DWTraderDB
            db = DWTraderDB()
        except Exception as exc:
            print(f"[WARN] DB unavailable ({exc!s:.120}) — running weather-only mode\n")

    target_date = (date.today() + timedelta(days=1)).isoformat()

    W = 100
    print()
    print("=" * W)
    print(f"  BRAIN COVERAGE REPORT — probe date: {target_date}")
    print("=" * W)

    # ── Section 1: KXHIGH per-city trace ─────────────────────────────────────
    print("\n[1] KXHIGH market trace — one sample ticker per _CITY_MAP entry\n")
    hdr = (
        f"{'CITY':<6} {'WX':>3} {'SIGMA_D':>8} {'SIGMA_F':>8} "
        f"{'BRIER':>7} {'N_B':>4} {'FCAST_F':>9} {'P_YES':>7}   REASON"
    )
    print(hdr)
    print("-" * W)

    results = []
    for city, (lat, lon, tz) in _CITY_MAP.items():
        r = await _probe_city(city, lat, lon, tz, target_date, db)
        results.append(r)

    # Sort: OK first, then by reason alphabetically
    for r in sorted(results, key=lambda x: (x["reason"] != "OK", x["reason"], x["city"])):
        sigma_d = str(r["sigma_days"])  if r["sigma_days"] is not None else "?"
        sigma_f = f"{r['sigma_val']:.2f}" if r["sigma_val"] is not None else "default"
        brier   = f"{r['brier_val']:.3f}" if r["brier_val"] is not None else "—"
        fcast   = f"{r['forecast_f']:.1f}" if r["forecast_f"] is not None else "None"
        pyes    = f"{r['p_yes']:.3f}" if r["p_yes"] is not None else "None"
        mark    = "OK" if r["reason"] == "OK" else "!!"
        print(
            f"{r['city']:<6} {'Y' if r['weather_ok'] else 'N':>3} "
            f"{sigma_d:>8} {sigma_f:>8} {brier:>7} {r['n_brier']:>4} "
            f"{fcast:>9} {pyes:>7}   {mark} {r['reason']}"
        )

    ok_cities   = [r for r in results if r["reason"] == "OK"]
    fail_cities = [r for r in results if r["reason"] != "OK"]
    print(f"\n  KXHIGH result: {len(ok_cities)}/{len(results)} cities produce P(YES)")
    if ok_cities:
        print(f"  Producing P(YES) : {', '.join(r['city'] for r in ok_cities)}")
    if fail_cities:
        by_reason: dict = {}
        for r in fail_cities:
            by_reason.setdefault(r["reason"], []).append(r["city"])
        for reason, cities in by_reason.items():
            print(f"  {reason:40s}: {', '.join(cities)}")

    # ── Section 2: KXTEMP city-code alias audit ───────────────────────────────
    print()
    print("[2] KXTEMP city-code mismatch — hourly markets parsed with wrong city code\n")
    print(
        "  Kalshi embeds city codes in KXTEMP tickers that differ from _CITY_MAP keys.\n"
        "  This causes estimate_p_yes to fail at the city-map check for every KXTEMP market.\n"
    )

    print(f"  {'TICKER EXAMPLE':<36} {'PARSED AS':<12} {'IN MAP?':<10} {'P_YES'}")
    print(f"  {'-'*36:<36} {'-'*12:<12} {'-'*10:<10} {'-'*6}")

    # Aliases seen in live logs (city code Kalshi uses -> _CITY_MAP key that should match)
    for alias, correct_key in _KXTEMP_ALIASES_SEEN.items():
        ticker  = _kxtemp_ticker(alias, target_date)
        parsed  = _parse_ticker(ticker)
        p_city  = parsed["city"] if parsed else "—"
        in_map  = p_city in _CITY_MAP
        pyes    = await estimate_p_yes(ticker, tau_hrs=24.0) if parsed else None
        verdict = "MISMATCH — should be " + correct_key
        print(f"  {ticker:<36} {p_city:<12} {'YES' if in_map else 'NO':<10} {pyes!r:<8}  <- {verdict}")

    # Spot-check: does the correct city code work?
    print()
    print("  Spot-check: KXTEMP with exact _CITY_MAP key (e.g. NYC, LAX)")
    for test_code in ["NYC", "LAX", "CHI"]:
        ticker = _kxtemp_ticker(test_code, target_date)
        parsed = _parse_ticker(ticker)
        in_map = parsed is not None and parsed.get("city") in _CITY_MAP
        pyes   = await estimate_p_yes(ticker, tau_hrs=24.0) if parsed else None
        mark   = "OK" if pyes is not None else "!! None"
        print(f"  KXTEMP{test_code:<10}  in_map={str(in_map):<5}  p_yes={pyes!r}  {mark}")

    # ── Section 3: KXRAIN ─────────────────────────────────────────────────────
    print()
    print("[3] KXRAIN market type — no parser exists\n")
    for prefix in _UNPARSED_PREFIXES:
        ticker = f"{prefix}NYC-{date.today().strftime('%y%b%d').upper()}-T0"
        parsed = _parse_ticker(ticker)
        print(f"  {ticker}")
        print(f"    _parse_ticker returned: {parsed!r}")
        print(f"    Reason : _parse_ticker has no regex for {prefix!r}")
        print(f"    Effect : ALL {prefix}* markets -> P(YES) = None, every cycle\n")

    # ── Section 4: DB calibration health ────────────────────────────────────
    if db is not None:
        print("[4] DB calibration health — per-city sigma and Brier\n")
        calibrated = [r for r in results if r["sigma_val"] is not None]
        no_sigma   = [r for r in results if r["sigma_days"] == 0]
        has_brier  = [r for r in results if r["brier_val"] is not None]

        print(f"  Cities with >= 14 AR(1) residual days (calibrated sigma): {len(calibrated)}")
        if calibrated:
            cal_labels = [r["city"] + f"({r['sigma_days']}d)" for r in calibrated]
            print(f"    {', '.join(cal_labels)}")
        print(f"  Cities with ZERO residual rows in DB (sigma = 4.0F default): {len(no_sigma)}")
        if no_sigma:
            print(f"    {', '.join(r['city'] for r in no_sigma)}")
        print(f"  Cities with rolling Brier data (>= 10 settled trades): {len(has_brier)}")
        if has_brier:
            for r in sorted(has_brier, key=lambda x: -(x["brier_val"] or 0)):
                print(f"    {r['city']:<6} brier={r['brier_val']:.3f} n={r['n_brier']}")

    # ── Root cause summary ────────────────────────────────────────────────────
    print()
    print("=" * W)
    print("  ROOT CAUSE SUMMARY")
    print("=" * W)

    fetch_fail = [r for r in results if "FORECAST_FETCH_FAIL" in r["reason"]]
    not_in_map = [r for r in results if "NOT_IN_MAP" in r["reason"]]
    parse_fail = [r for r in results if r["reason"] == "TICKER_PARSE_FAIL"]
    unknown    = [r for r in results if r["reason"].startswith("UNKNOWN")]

    cause_lines = []
    if ok_cities:
        cause_lines.append(
            f"  [OK]  {len(ok_cities)} {'city produces' if len(ok_cities)==1 else 'cities produce'} "
            f"P(YES): {', '.join(r['city'] for r in ok_cities)}"
        )
    if fetch_fail:
        cause_lines.append(
            f"  [A]   FORECAST_FETCH_FAIL ({len(fetch_fail)} cities) — Open-Meteo returned None.\n"
            f"        Cities: {', '.join(r['city'] for r in fetch_fail)}\n"
            f"        Likely causes:\n"
            f"          • Target date beyond Open-Meteo's 7-day horizon\n"
            f"          • Transient rate-limit from ~19 concurrent fetches at cycle start\n"
            f"          • Open-Meteo API timeout (observed repeatedly on May 30-31)"
        )
    if not_in_map:
        cause_lines.append(
            f"  [B]   CITY_NOT_IN_MAP ({len(not_in_map)} cities): {', '.join(r['city'] for r in not_in_map)}"
        )
    if parse_fail:
        cause_lines.append(
            f"  [C]   TICKER_PARSE_FAIL ({len(parse_fail)} cities) — should not happen for KXHIGH tickers"
        )
    if unknown:
        cause_lines.append(
            f"  [D]   UNKNOWN ({len(unknown)} cities) — inputs OK but estimate_p_yes still None"
        )

    cause_lines.append(
        "  [E]   MARKET TYPE GAPS — bulk of the 663 'No P(YES)' in the trade funnel:\n"
        "          KXRAIN*   -> _parse_ticker has no KXRAIN regex -> always None\n"
        "          KXTEMPNYCH -> city='NYCH' not in _CITY_MAP (should map to NYC coords)\n"
        "          Other KXTEMP* codes may have the same mismatch.\n"
        "          These two gap types likely account for 500-600 of the 945 markets\n"
        "          scanned each cycle that never produce an estimate."
    )

    print()
    for line in cause_lines:
        print(line)
    print()

    print("  NEXT STEPS:")
    print("  1. If [A]: add a retry/cache warm-up before the first trade cycle,")
    print("     or fetch forecasts in the weather loop and persist to DB.")
    print("  2. If [E]: either add KXTEMP alias mappings to _CITY_MAP")
    print("     (e.g. 'NYCH': (40.7128, -74.006, 'America/New_York'))")
    print("     or add a KXRAIN parser if rain markets are worth trading.")
    print("  3. Check Kalshi's actual market universe: are KXHIGH markets available")
    print("     for cities other than LAX/TDC? If not, the brain only covers 2 cities")
    print("     by design and the KXTEMP alias fix is the highest-leverage change.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
