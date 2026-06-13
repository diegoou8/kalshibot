"""
Weather-based probability estimator for Kalshi temperature markets.

Fetches Open-Meteo forecasts, maps them to Kalshi settlement conditions,
and returns P(YES settles) as an independent prior for the brain.

Supported ticker formats:
  KXHIGH{CITY}-{YYMMMDD}-B{MID}.5     daily high band [floor(mid), ceil(mid))
  KXHIGH{CITY}-{YYMMMDD}-T{THRESH}    daily high above/below threshold
  KXTEMP{CITY}-{YYMMMDD}{HH}-T{THRESH} hourly temp above threshold
"""
import json
import math
import re
import logging
import asyncio
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

from src.types import ContractSemantics

import aiohttp
from scipy.stats import t as _student_t

logger = logging.getLogger(__name__)

# Kalshi city code → (lat, lon, timezone)
_CITY_MAP: Dict[str, Tuple[float, float, str]] = {
    "NYC":  (40.7128, -74.0060, "America/New_York"),
    "CHI":  (41.8781, -87.6298, "America/Chicago"),
    "LAX":  (34.0522, -118.2437, "America/Los_Angeles"),
    "MIA":  (25.7617, -80.1918, "America/New_York"),
    "SEA":  (47.6062, -122.3321, "America/Los_Angeles"),
    "DAL":  (32.7767, -96.7970,  "America/Chicago"),
    "HOU":  (29.7604, -95.3698,  "America/Chicago"),
    "THOU": (29.7604, -95.3698,  "America/Chicago"),
    "BOS":  (42.3601, -71.0589,  "America/New_York"),
    "BOSH": (42.3601, -71.0589,  "America/New_York"),
    "SFO":  (37.7749, -122.4194, "America/Los_Angeles"),
    "DEN":  (39.7392, -104.9903, "America/Denver"),
    "PHX":  (33.4484, -112.0740, "America/Phoenix"),
    "ATL":  (33.7490, -84.3880,  "America/New_York"),
    "TDC":  (38.9072, -77.0369,  "America/New_York"),
    "PHIL": (39.9526, -75.1652,  "America/New_York"),
    "SAT":  (29.4241, -98.4936,  "America/Chicago"),
    "MIN":  (44.9778, -93.2650,  "America/Chicago"),
    "OKC":  (35.4676, -97.5164,  "America/Chicago"),
    "NOLA": (29.9511, -90.0715,  "America/Chicago"),    # New Orleans, LA
    "AUS":  (30.2672, -97.7431,  "America/Chicago"),    # Austin, TX
    "LV":   (36.1699, -115.1398, "America/Los_Angeles"), # Las Vegas, NV
}

# Forecast uncertainty σ (°F) — reflects ensemble spread + model error.
# Per-city/horizon MLE fitting is the target; 4.0 is the current calibrated default.
_FORECAST_SIGMA_F = 4.0

# Student-t degrees of freedom — heavier tails than Gaussian to handle
# heatwave/cold-snap extremes (ν=7 ≈ industry standard for NWP tail risk).
_STUDENT_T_DF = 7

# AR(1) coefficient for forecast bias correction.
# e_{t-1} = actual_yesterday − forecast_yesterday; correction = φ × e_{t-1}
# Estimated from historical residuals; 0.4 is a reasonable NWP starting point.
_AR1_PHI = 0.4

_SIGMA_BY_HORIZON_PATH = Path(__file__).resolve().parents[2] / "data" / "sigma_by_horizon.json"

# Cache: cache_key → (forecast_temp_f, date_fetched)
_forecast_cache: Dict[str, float] = {}
# AR(1) metadata cache: "ar1:{lat},{lon}" → {correction, e_prev, actual_yest, forecast_yest}
_ar1_error_cache: Dict[str, Dict] = {}

# Adaptive bias filter — module-level singleton so state persists across calls
# within a process lifetime. Starts cold (bias=0 for all cities).
from src.brain.bias_filter import AdaptiveBiasFilter as _AdaptiveBiasFilter
_adaptive_bias = _AdaptiveBiasFilter(k_base=0.3, k_max=0.7, window=5)

# Reverse lookup: "lat,lon" → city code, derived from _CITY_MAP.
# Built once at module load; used by _fetch_ar1_correction and estimate_p_yes.
_LAT_LON_TO_CITY: Dict[str, str] = {
    f"{lat:.3f},{lon:.3f}": city
    for city, (lat, lon, _tz) in _CITY_MAP.items()
}

# ── City-code alias maps ──────────────────────────────────────────────────────
# Kalshi uses different city suffixes across market types (KXTEMP vs KXHIGH) and
# has introduced T-prefixed variants and new cities over time.  Any suffix not
# found in _CITY_MAP is looked up here before estimate_p_yes is attempted.
# If still not found, estimate_p_yes returns None and a warning is logged once
# per process lifetime (via _unknown_city_logged) to avoid per-market spam.

# KXTEMP hourly-temp markets use a different city code than KXHIGH daily-high markets.
_KXTEMP_CITY_ALIAS: Dict[str, str] = {
    "NYCH": "NYC",   # KXTEMPNYCH-... (48 markets observed 2026-06-01)
}

# KXHIGH daily-high markets have accumulated T-prefixed variants and new cities
# that are not yet in _CITY_MAP.  Alias maps to existing _CITY_MAP entry.
_KXHIGH_CITY_ALIAS: Dict[str, str] = {
    # T-prefix variants — same city, different Kalshi abbreviation
    "NY":    "NYC",   # KXHIGHNY-... (NY used instead of NYC)
    "TSFO":  "SFO",
    "TPHX":  "PHX",
    "TMIN":  "MIN",
    "TATL":  "ATL",
    "TOKC":  "OKC",
    "TBOS":  "BOS",
    "TSATX": "SAT",
    "TSEA":  "SEA",
    "TDAL":  "DAL",
    # New cities added to Kalshi after initial _CITY_MAP was written.
    # Coordinates are best-available city-centre approximations.
    "TNOLA": "NOLA",  # New Orleans, LA  — see _CITY_MAP entry above
    "AUS":   "AUS",   # Austin, TX       — see _CITY_MAP entry above
    "TLV":   "LV",    # Las Vegas, NV    — see _CITY_MAP entry above
}

# City codes already emitted as unknown warnings (once per process lifetime).
_unknown_city_logged: set = set()


def normalize_city_code(raw_code: str, market_prefix: Optional[str] = None) -> str:
    """
    Resolve a Kalshi city suffix to its canonical _CITY_MAP key.

    raw_code:      city code as extracted from a ticker (e.g. 'TSFO', 'NYCH', 'LAX').
    market_prefix: optional hint — 'KXHIGH' or 'KXTEMP' — selects the right alias
                   map when the same raw code could theoretically appear in both types.
                   When None, KXHIGH is tried first, then KXTEMP (disjoint today).

    Returns the canonical key (e.g. 'SFO', 'NYC', 'LAX'). If no alias resolves the
    code it is returned unchanged (uppercased) and a one-time WARNING is emitted with
    the appropriate UNKNOWN_*_CITY_CODE tag to prevent per-market log spam.

    This is the single authoritative normalization point.  _parse_ticker() and
    _ticker_city() both delegate to this function; add new aliases only here.
    """
    code = raw_code.upper()
    if code in _CITY_MAP:
        return code

    prefix_up = (market_prefix or "").upper()
    if "TEMP" in prefix_up:
        normed = _KXTEMP_CITY_ALIAS.get(code, code)
    elif "HIGH" in prefix_up:
        normed = _KXHIGH_CITY_ALIAS.get(code, code)
    else:
        normed = _KXHIGH_CITY_ALIAS.get(code) or _KXTEMP_CITY_ALIAS.get(code) or code

    if normed not in _CITY_MAP and normed not in _unknown_city_logged:
        tag = (
            "UNKNOWN_KXTEMP_CITY_CODE" if "TEMP" in prefix_up else
            "UNKNOWN_KXHIGH_CITY_CODE" if "HIGH" in prefix_up else
            "UNKNOWN_CITY_CODE"
        )
        logger.warning(
            "%s: raw=%s resolved=%s — not in _CITY_MAP, add to alias map",
            tag, code, normed,
        )
        _unknown_city_logged.add(normed)
    return normed


def _log_alias_startup() -> None:
    logger.info(
        "City alias maps loaded — KXHIGH: %d aliases, KXTEMP: %d alias, total: %d",
        len(_KXHIGH_CITY_ALIAS), len(_KXTEMP_CITY_ALIAS),
        len(_KXHIGH_CITY_ALIAS) + len(_KXTEMP_CITY_ALIAS),
    )


_log_alias_startup()


def _tau_to_bin(tau_hrs: float) -> str:
    if tau_hrs < 6:   return "0-6h"
    if tau_hrs < 12:  return "6-12h"
    if tau_hrs < 24:  return "12-24h"
    if tau_hrs < 48:  return "24-48h"
    return "48h+"


def load_horizon_sigma(path: str = str(_SIGMA_BY_HORIZON_PATH)) -> Dict[str, float]:
    """Load per-horizon-bin σ from JSON file. Returns {} if file does not exist."""
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _p_above(forecast_temp: float, threshold: float, sigma: float = _FORECAST_SIGMA_F) -> float:
    """
    P(actual_temp > threshold) using Student-t distribution.
    Heavier tails than Gaussian — correctly handles extreme weather events.
    """
    # z = (threshold - forecast) / sigma so that P(T_actual > threshold)
    # = P(forecast + sigma*T_7 > threshold) = P(T_7 > z) = sf(z)
    z = (threshold - forecast_temp) / sigma
    p = float(_student_t.sf(z, df=_STUDENT_T_DF))
    clamped = max(0.03, min(0.97, p))
    if clamped != p:
        logger.debug(
            "PROB_CLAMP: raw=%.5f → %.3f (threshold=%.1f, forecast=%.1f, sigma=%.2f)",
            p, clamped, threshold, forecast_temp, sigma,
        )
    return clamped


def _parse_ticker(ticker: str) -> Optional[Dict]:
    """
    Parse a Kalshi weather market ticker into components.
    Returns None if unrecognised format.
    """
    # KXHIGH{CITY}-{YYMMMDD}-B{LOWER}.5  (band market)
    m = re.match(
        r"KXHIGH([A-Z]+)-(\d{2}[A-Z]{3}\d{2})-B(\d+(?:\.\d+)?)",
        ticker, re.IGNORECASE
    )
    if m:
        city = normalize_city_code(m.group(1).upper(), market_prefix="KXHIGH")
        date_str = m.group(2).upper()
        # Ticker uses the midpoint of a 1-degree band (e.g. B87.5 → [87, 88))
        mid = float(m.group(3))
        lower = float(math.floor(mid))
        upper = float(math.ceil(mid))
        return {"type": "HIGH_BAND", "city": city, "date_str": date_str,
                "lower": lower, "upper": upper, "hour": None}

    # KXHIGH{CITY}-{YYMMMDD}-T{THRESH}  (above/below)
    m = re.match(
        r"KXHIGH([A-Z]+)-(\d{2}[A-Z]{3}\d{2})-T(\d+(?:\.\d+)?)",
        ticker, re.IGNORECASE
    )
    if m:
        city = normalize_city_code(m.group(1).upper(), market_prefix="KXHIGH")
        date_str, thresh = m.group(2).upper(), float(m.group(3))
        return {"type": "HIGH_ABOVE", "city": city, "date_str": date_str,
                "threshold": thresh, "hour": None}

    # KXTEMP{CITY}-{YYMMMDD}{HH}-T{THRESH}  (hourly temp)
    m = re.match(
        r"KXTEMP([A-Z]+)-(\d{2}[A-Z]{3}\d{2})(\d{2})-T(\d+(?:\.\d+)?)",
        ticker, re.IGNORECASE
    )
    if m:
        city = normalize_city_code(m.group(1).upper(), market_prefix="KXTEMP")
        date_str, hour, thresh = (
            m.group(2).upper(), int(m.group(3)), float(m.group(4))
        )
        return {"type": "HOURLY_ABOVE", "city": city, "date_str": date_str,
                "threshold": thresh, "hour": hour}

    return None


def _parse_date(date_str: str) -> Optional[str]:
    """Convert '26APR20' → '2026-04-20'."""
    try:
        dt = datetime.strptime(date_str, "%y%b%d")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


async def _fetch_daily_max(lat: float, lon: float, tz: str, target_date: str) -> Optional[float]:
    """Fetch daily max temperature (°F) from Open-Meteo for a given date."""
    cache_key = f"{lat:.3f},{lon:.3f},{target_date}"
    if cache_key in _forecast_cache:
        return _forecast_cache[cache_key]

    params = {
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": tz,
        "start_date": target_date,
        "end_date": target_date,
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            async with session.get(
                "https://api.open-meteo.com/v1/forecast", params=params
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                temps = data.get("daily", {}).get("temperature_2m_max", [])
                if temps and temps[0] is not None:
                    val = float(temps[0])
                    _forecast_cache[cache_key] = val
                    return val
    except Exception as e:
        logger.debug("Open-Meteo fetch failed for %s: %s", cache_key, e)
    return None


async def _fetch_hourly_temp(
    lat: float, lon: float, tz: str, target_date: str, hour: int
) -> Optional[float]:
    """Fetch hourly temperature (°F) from Open-Meteo for a given date+hour."""
    cache_key = f"{lat:.3f},{lon:.3f},{target_date}T{hour:02d}"
    if cache_key in _forecast_cache:
        return _forecast_cache[cache_key]

    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": tz,
        "start_date": target_date,
        "end_date": target_date,
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            async with session.get(
                "https://api.open-meteo.com/v1/forecast", params=params
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                times = data.get("hourly", {}).get("time", [])
                temps = data.get("hourly", {}).get("temperature_2m", [])
                for t, temp in zip(times, temps):
                    if t.endswith(f"T{hour:02d}:00") and temp is not None:
                        val = float(temp)
                        _forecast_cache[cache_key] = val
                        return val
    except Exception as e:
        logger.debug("Open-Meteo hourly fetch failed: %s", e)
    return None


async def _fetch_ar1_correction(lat: float, lon: float, tz: str) -> float:
    """
    AR(1) bias correction: φ × e_{t-1}, where e_{t-1} = actual_temp − forecast_temp
    for yesterday. Corrects the well-known NWP serial correlation in errors.

    Returns an additive °F correction to apply to today's raw forecast.
    Returns 0.0 on any fetch failure (graceful degradation).
    """
    cache_key = f"ar1:{lat:.3f},{lon:.3f}"
    if cache_key in _ar1_error_cache:
        return _ar1_error_cache[cache_key]["correction"]

    yesterday = (date.today() - timedelta(days=1)).isoformat()

    # Forecast for yesterday (what the model said yesterday about yesterday)
    forecast_yest = await _fetch_daily_max(lat, lon, tz, yesterday)
    if forecast_yest is None:
        return 0.0

    # Actual for yesterday — query Open-Meteo archive API
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": tz,
        "start_date": yesterday,
        "end_date": yesterday,
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(
                "https://archive-api.open-meteo.com/v1/archive", params=params
            ) as resp:
                if resp.status != 200:
                    return 0.0
                data = await resp.json()
                temps = data.get("daily", {}).get("temperature_2m_max", [])
                if not temps or temps[0] is None:
                    return 0.0
                actual_yest = float(temps[0])
    except Exception as e:
        logger.debug("AR(1) archive fetch failed for %s,%s: %s", lat, lon, e)
        return 0.0

    e_prev = actual_yest - forecast_yest
    correction = _AR1_PHI * e_prev
    _ar1_error_cache[cache_key] = {
        "correction":    correction,
        "e_prev":        e_prev,
        "actual_yest":   actual_yest,
        "forecast_yest": forecast_yest,
        "yesterday":     yesterday,
    }
    logger.debug("AR(1) correction for (%.3f,%.3f): actual=%.1f forecast=%.1f e=%.1f corr=%.2f",
                 lat, lon, actual_yest, forecast_yest, e_prev, correction)

    # Feed the new observation into the adaptive bias filter.
    city_code = _LAT_LON_TO_CITY.get(f"{lat:.3f},{lon:.3f}")
    if city_code is not None:
        _adaptive_bias.update(city_code, actual_yest, forecast_yest)

    return correction


def get_ar1_metadata(lat: float, lon: float) -> Optional[Dict]:
    """Return cached AR(1) residual data for a city (for DB persistence by callers)."""
    cache_key = f"ar1:{lat:.3f},{lon:.3f}"
    return _ar1_error_cache.get(cache_key)


def load_city_params(db, min_days: int = 14) -> Dict[str, Dict]:
    """
    Load per-city calibrated σ and φ from DB residuals.
    Falls back to module defaults when fewer than min_days of data are available.
    Returns: {city_code: {"sigma": float, "phi": float, "calibrated": bool}}

    Call once per trade cycle — fast (SQLite reads only, no network I/O).
    """
    params: Dict[str, Dict] = {}
    for city in _CITY_MAP:
        phi = db.get_ar1_phi_estimate(city, min_days=min_days)
        sigma = db.get_sigma_mle(city, min_days=min_days)
        params[city] = {
            "sigma":      sigma if sigma is not None else _FORECAST_SIGMA_F,
            "phi":        phi   if phi   is not None else _AR1_PHI,
            "calibrated": sigma is not None and phi is not None,
        }
    return params


def get_forecast_temp_for_ticker(ticker: str) -> Optional[float]:
    """Return the cached forecast temperature for a ticker.
    Only valid after estimate_p_yes() has been called for this ticker.
    Returns None if ticker is unparseable or not yet cached.
    """
    parsed = _parse_ticker(ticker)
    if not parsed:
        return None
    city = parsed["city"]
    if city not in _CITY_MAP:
        return None
    lat, lon, _ = _CITY_MAP[city]
    iso_date = _parse_date(parsed["date_str"])
    if not iso_date:
        return None
    hour = parsed.get("hour")
    if hour is not None:
        cache_key = f"{lat:.3f},{lon:.3f},{iso_date}T{hour:02d}"
    else:
        cache_key = f"{lat:.3f},{lon:.3f},{iso_date}"
    return _forecast_cache.get(cache_key)


async def estimate_p_yes(
    semantics: ContractSemantics,
    sigma_f: Optional[float] = None,
    phi: Optional[float] = None,
    tau_hrs: Optional[float] = None,
) -> Optional[float]:
    """
    Returns P(YES settles) for a Kalshi weather market based on Open-Meteo forecast.
    Takes verified ContractSemantics — direction, bounds, and city are resolved.

    BAND:      P(floor_strike <= T < cap_strike)
    ABOVE:     P(T > threshold)
    BELOW:     1 - P(T > threshold)

    sigma_f:  forecast uncertainty (°F). Uses per-city MLE when provided, else module default.
    phi:      AR(1) coefficient. Uses per-city OLS when provided, else module default.
    tau_hrs:  hours to settlement; selects horizon-conditioned σ from sigma_by_horizon.json.
    """
    city = semantics.canonical_city
    if not city or city not in _CITY_MAP:
        return None

    lat, lon, tz = _CITY_MAP[city]
    iso_date = semantics.settlement_date
    if not iso_date:
        return None

    _horizon_sigma = load_horizon_sigma()
    if tau_hrs is not None and _horizon_sigma:
        tau_bin = _tau_to_bin(tau_hrs)
        sigma_use = _horizon_sigma.get(tau_bin, sigma_f if sigma_f is not None else _FORECAST_SIGMA_F)
    else:
        sigma_use = sigma_f if sigma_f is not None else _FORECAST_SIGMA_F
    phi_use = phi if phi is not None else _AR1_PHI

    # AR(1) bias correction — fetch once per city per run (cached)
    await _fetch_ar1_correction(lat, lon, tz)
    cache_key = f"ar1:{lat:.3f},{lon:.3f}"
    meta = _ar1_error_cache.get(cache_key)
    e_prev = meta["e_prev"] if meta else 0.0
    city_code = _LAT_LON_TO_CITY.get(f"{lat:.3f},{lon:.3f}")
    ar1 = _adaptive_bias.correction(city_code) if city_code else phi_use * e_prev

    contract_type = semantics.contract_type  # BAND | THRESHOLD | HOURLY

    # Gumbel σ correction for daily-max markets (KXHIGH settles on daily max, not point-in-time)
    if contract_type in ("BAND", "THRESHOLD"):
        from src.config.env import Config as _Cfg
        _gumbel_mode = getattr(_Cfg, "GUMBEL_MODE", "half")
        _sigma_intraday = 2.0
        if _gumbel_mode != "none":
            _scale = 0.5 if _gumbel_mode == "half" else 1.0
            _sigma_eff = _sigma_intraday * _scale
            _gumbel_std = _sigma_eff * math.pi / math.sqrt(6)
            sigma_use = math.sqrt(sigma_use ** 2 + _gumbel_std ** 2)
        logger.debug("T_max sigma: %.3f°F (gumbel_mode=%s)", sigma_use, _gumbel_mode)

    if contract_type == "BAND":
        temp = await _fetch_daily_max(lat, lon, tz, iso_date)
        if temp is None:
            return None
        temp_adj = temp + ar1
        p_above_lower = _p_above(temp_adj, semantics.floor_strike, sigma_use)
        p_above_upper = _p_above(temp_adj, semantics.cap_strike, sigma_use)
        p_band = p_above_lower - p_above_upper
        clamped = max(0.03, min(0.97, p_band))
        if clamped != p_band:
            logger.debug("PROB_CLAMP_BAND %s: raw=%.5f -> %.3f", semantics.ticker, p_band, clamped)
        return float(clamped)

    elif contract_type == "THRESHOLD":
        temp = await _fetch_daily_max(lat, lon, tz, iso_date)
        if temp is None:
            return None
        temp_adj = temp + ar1
        p_above = _p_above(temp_adj, semantics.threshold, sigma_use)
        if semantics.direction == "BELOW":
            return 1.0 - p_above
        return p_above  # direction == "ABOVE"

    elif contract_type == "HOURLY":
        temp = await _fetch_hourly_temp(lat, lon, tz, iso_date, semantics.settlement_hour)
        if temp is None:
            return None
        return _p_above(temp + ar1, semantics.threshold, sigma_use)

    return None
