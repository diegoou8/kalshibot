"""
Contract Semantics Audit — parse and verify Kalshi weather ticker semantics.

Resolves the six open questions from the Jun-13 post-mortem:
  1. Does B87.5 mean daily high > 87.5, or high IN [87.5, 88.5)?
  2. Does T88 mean high >= 88 (above) or the specific bucket "temperature at 88"?
  3. KXHIGH vs KXTEMP: different settlement definitions?
  4. KXHIGH uses daily max; KXTEMP uses hourly temp — confirmed by weather_estimator.py.
  5. Dates are local-city timezone (Open-Meteo is requested with city tz).
  6. Are temperatures consistently Fahrenheit?

Answers encoded in this module (authoritative reference):
  KXHIGH{CITY}-{YYMMMDD}-B{MID}.5 → YES if daily_max_F ∈ [floor(MID), ceil(MID)).
                                     Kalshi uses floor_strike / cap_strike; MID is midpoint.
  KXHIGH{CITY}-{YYMMMDD}-T{N}     → YES if daily_max_F > N  (strike_type="greater")
                                     OR  daily_max_F < N  (strike_type="less").
                                     Direction CANNOT be inferred from ticker — requires metadata.
  KXTEMP{CITY}-{YYMMMDD}{HH}-T{N} → YES if hourly_temp_F at hour HH >= N. Local city time.

Source of truth:
  Kalshi settlement: https://kalshi.com/rules/weather (as observed and backtested).
  Open-Meteo: temperature_2m_max for KXHIGH, temperature_2m for KXTEMP (hourly).
  Units: Fahrenheit everywhere (Open-Meteo requested with temperature_unit=fahrenheit).
  Timezone: local to city (Open-Meteo requested with timezone=America/{city_tz}).

Audit warnings generated for:
  - Unknown city code (no lat/lon available → model cannot estimate p_yes)
  - KXTEMP with hour > 23 or hour < 0
  - Threshold outside credible range for city/season (< -30°F or > 130°F)
  - Ticker prefix not in {KXHIGH, KXTEMP}
  - City alias mismatch (ticker city != canonical city after normalization)
"""
import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.types import ContractSemantics

logger = logging.getLogger(__name__)

# Maps Kalshi strike_type to direction strings our model uses
_STRIKE_TYPE_TO_DIRECTION: Dict[str, str] = {
    "greater":           "ABOVE",   # YES if daily_max > floor_strike
    "greater_or_equal":  "ABOVE",
    "less":              "BELOW",   # YES if daily_max < cap_strike
    "less_or_equal":     "BELOW",
    "between":           "BAND",    # YES if floor_strike <= daily_max < cap_strike
}

# Import the canonical alias maps and city map from weather_estimator
from src.brain.weather_estimator import (
    _CITY_MAP,
    _KXHIGH_CITY_ALIAS,
    _KXTEMP_CITY_ALIAS,
    normalize_city_code,
    _parse_ticker,
)

# Temperature sanity range (°F) — anything outside this is flagged
_TEMP_MIN_F = -30.0
_TEMP_MAX_F = 130.0


def audit_ticker(ticker: str) -> Dict:
    """
    Parse a Kalshi ticker and return a full semantics audit dict.

    Keys:
        ticker, market_type, city, canonical_city, settle_date,
        hour_local, threshold_f, direction, bucket_lower_f, bucket_upper_f,
        timezone_city, temp_source, settlement_rule, warnings, valid
    """
    warnings: List[str] = []
    ticker_up = ticker.upper().strip()

    # ── Determine market prefix ───────────────────────────────────────────────
    if ticker_up.startswith("KXHIGH"):
        prefix = "KXHIGH"
    elif ticker_up.startswith("KXTEMP"):
        prefix = "KXTEMP"
    else:
        return {
            "ticker": ticker,
            "valid": False,
            "warnings": [f"UNKNOWN_PREFIX: ticker does not start with KXHIGH or KXTEMP"],
        }

    # ── Delegate parse to weather_estimator._parse_ticker ────────────────────
    parsed = _parse_ticker(ticker_up)
    if parsed is None:
        return {
            "ticker": ticker,
            "market_type": None,
            "valid": False,
            "warnings": [f"PARSE_FAILED: unrecognised ticker format"],
        }

    market_type   = parsed.get("type")       # HIGH_BAND | HIGH_ABOVE | HOURLY_ABOVE
    city          = parsed.get("city")        # already normalized via normalize_city_code
    date_str      = parsed.get("date_str")    # e.g. "26JUN07"
    hour_local    = parsed.get("hour")        # None for KXHIGH
    threshold_f   = parsed.get("threshold")   # for HIGH_ABOVE / HOURLY_ABOVE
    bucket_lower  = parsed.get("lower")       # for HIGH_BAND
    bucket_upper  = parsed.get("upper")       # for HIGH_BAND

    # ── Canonical city + timezone ─────────────────────────────────────────────
    city_info     = _CITY_MAP.get(city, {})
    has_city      = bool(city_info)
    if not has_city:
        warnings.append(f"UNKNOWN_CITY: '{city}' not in _CITY_MAP — model cannot estimate p_yes")

    tz = city_info[2] if has_city else None

    # ── Settle date parse ─────────────────────────────────────────────────────
    settle_date = None
    if date_str:
        try:
            settle_date = datetime.strptime(date_str, "%y%b%d").strftime("%Y-%m-%d")
        except ValueError:
            warnings.append(f"DATE_PARSE_FAILED: '{date_str}' is not valid %y%b%d")

    # ── Hour validation (KXTEMP only) ─────────────────────────────────────────
    if market_type == "HOURLY_ABOVE":
        if hour_local is None:
            warnings.append("HOUR_MISSING: KXTEMP ticker has no hour component")
        elif not (0 <= hour_local <= 23):
            warnings.append(f"HOUR_OUT_OF_RANGE: hour={hour_local} not in [0, 23]")

    # ── Temperature range sanity ──────────────────────────────────────────────
    check_temps = [t for t in [threshold_f, bucket_lower, bucket_upper] if t is not None]
    for t in check_temps:
        if t < _TEMP_MIN_F or t > _TEMP_MAX_F:
            warnings.append(
                f"TEMP_OUT_OF_RANGE: {t:.1f}°F outside [{_TEMP_MIN_F}, {_TEMP_MAX_F}]"
            )

    # ── Resolve settlement semantics ─────────────────────────────────────────
    temp_source = None
    direction   = None
    settlement_rule = None

    if market_type == "HIGH_BAND":
        # B87.5 → YES if daily_max_F ∈ [87.5, 88.5)
        temp_source = "daily_max_F (Open-Meteo temperature_2m_max, Fahrenheit)"
        direction = "BAND"
        if bucket_lower is not None and bucket_upper is not None:
            settlement_rule = (
                f"YES if city={city} daily max temperature (°F) is in "
                f"[{bucket_lower:.1f}, {bucket_upper:.1f}). "
                f"Date is {settle_date} local {tz}. "
                f"Settlement source: Open-Meteo daily max; Kalshi settles on NWS observed max."
            )

    elif market_type == "HIGH_ABOVE":
        # T88 → YES if daily_max_F >= 88
        temp_source = "daily_max_F (Open-Meteo temperature_2m_max, Fahrenheit)"
        direction = "ABOVE"
        if threshold_f is not None:
            settlement_rule = (
                f"YES if city={city} daily max temperature (°F) >= {threshold_f:.1f}. "
                f"Date is {settle_date} local {tz}. "
                f"Settlement source: Open-Meteo daily max; Kalshi settles on NWS observed max."
            )

    elif market_type == "HOURLY_ABOVE":
        # T88 on KXTEMP → YES if temp at hour HH >= 88
        temp_source = "hourly_temp_F (Open-Meteo temperature_2m, Fahrenheit, hourly)"
        direction = "ABOVE"
        if threshold_f is not None and hour_local is not None:
            settlement_rule = (
                f"YES if city={city} hourly temperature (°F) at hour {hour_local:02d}:00 "
                f"local time >= {threshold_f:.1f}. "
                f"Date is {settle_date} local {tz}. "
                f"Settlement source: Open-Meteo hourly temp_2m."
            )

    # ── NWS vs Open-Meteo discrepancy warning ────────────────────────────────
    if market_type in ("HIGH_BAND", "HIGH_ABOVE"):
        warnings.append(
            "MODEL_SOURCE_MISMATCH_RISK: Kalshi KXHIGH settles on NWS observed "
            "daily max; model uses Open-Meteo forecast. In summer, NWS airport "
            "stations may read 1-3°F above/below Open-Meteo grid point — check "
            "for city-specific bias in calibration_diagnostics."
        )

    return {
        "ticker":          ticker,
        "market_type":     market_type,
        "city":            city,
        "settle_date":     settle_date,
        "hour_local":      hour_local,
        "threshold_f":     threshold_f,
        "direction":       direction,
        "bucket_lower_f":  bucket_lower,
        "bucket_upper_f":  bucket_upper,
        "timezone_city":   tz,
        "temp_source":     temp_source,
        "settlement_rule": settlement_rule,
        "warnings":        warnings,
        "valid":           has_city and settlement_rule is not None,
    }


def audit_and_store(ticker: str, db) -> Dict:
    """
    Run audit_ticker() and persist the result to contract_semantics table.
    Returns the audit dict.
    """
    result = audit_ticker(ticker)
    if result.get("warnings"):
        for w in result["warnings"]:
            logger.info("CONTRACT_AUDIT [%s]: %s", ticker, w)

    try:
        db.upsert_contract_semantics(
            ticker          = ticker,
            market_type     = result.get("market_type"),
            city            = result.get("city"),
            settle_date     = result.get("settle_date"),
            hour_local      = result.get("hour_local"),
            threshold_f     = result.get("threshold_f"),
            direction       = result.get("direction"),
            bucket_lower_f  = result.get("bucket_lower_f"),
            bucket_upper_f  = result.get("bucket_upper_f"),
            timezone_city   = result.get("timezone_city"),
            temp_source     = result.get("temp_source"),
            settlement_rule = result.get("settlement_rule"),
            audit_warnings  = json.dumps(result.get("warnings", [])),
        )
    except Exception as e:
        logger.error("CONTRACT_AUDIT_STORE_FAILED [%s]: %s", ticker, e)

    return result


# ---------------------------------------------------------------------------
# Metadata validation — compares parser interpretation against Kalshi REST
# ---------------------------------------------------------------------------

def _compare_parser_to_metadata(audit: Dict, meta: Dict) -> Tuple[bool, str, Optional[str]]:
    """
    Compare audit_ticker() output to live Kalshi market metadata.

    Returns:
        (matches: bool, direction_meta: str, mismatch_details: Optional[str])

    direction_meta is authoritative direction from Kalshi ("ABOVE", "BELOW", "BAND").
    mismatch_details is a JSON string listing fields that do not agree.

    matches=False only for STRUCTURAL mismatches (bucket bounds wrong, market type
    inverted). ABOVE/BELOW polarity mismatch for T-type markets is auto-correctable
    via direction_meta — those are NOT flagged as unsupported.
    """
    structural_mismatches: List[str] = []
    info_notes: List[str] = []

    strike_type = (meta.get("strike_type") or "").lower()
    direction_meta = _STRIKE_TYPE_TO_DIRECTION.get(strike_type)

    if direction_meta is None:
        info_notes.append(f"UNKNOWN_STRIKE_TYPE: '{strike_type}'")
        direction_meta = audit.get("direction") or "ABOVE"

    parser_dir = audit.get("direction")

    # ── Market-type structural check ──────────────────────────────────────────
    # Parser saying BAND but meta says ABOVE/BELOW (or vice versa) is structural.
    # Parser saying ABOVE but meta says BELOW is just polarity — auto-correctable.
    if direction_meta == "BAND" and parser_dir != "BAND":
        structural_mismatches.append(
            f"MARKET_TYPE_MISMATCH: parser={parser_dir} expected BAND per metadata"
        )
    elif direction_meta in ("ABOVE", "BELOW") and parser_dir == "BAND":
        structural_mismatches.append(
            f"MARKET_TYPE_MISMATCH: parser=BAND expected {direction_meta} per metadata"
        )
    elif direction_meta in ("ABOVE", "BELOW") and parser_dir and direction_meta != parser_dir:
        # Polarity mismatch (ABOVE vs BELOW) — stored as info, auto-corrected via direction_meta
        info_notes.append(f"POLARITY_CORRECTED: parser={parser_dir}, meta={direction_meta}")

    # ── Bucket bounds (BAND markets only) ────────────────────────────────────
    if direction_meta == "BAND" and parser_dir == "BAND":
        kx_floor = meta.get("floor_strike")
        kx_cap   = meta.get("cap_strike")
        our_lower = audit.get("bucket_lower_f")
        our_upper = audit.get("bucket_upper_f")
        # Guard: skip comparison for clearly bogus API values (< 1°F for temperature)
        if kx_floor is not None and float(kx_floor) >= 1.0 and our_lower is not None:
            if abs(float(kx_floor) - float(our_lower)) > 0.5:
                structural_mismatches.append(
                    f"BUCKET_LOWER_MISMATCH: parser={our_lower}, meta={kx_floor}"
                )
        if kx_cap is not None and float(kx_cap) >= 1.0 and our_upper is not None:
            if abs(float(kx_cap) - float(our_upper)) > 0.5:
                structural_mismatches.append(
                    f"BUCKET_UPPER_MISMATCH: parser={our_upper}, meta={kx_cap}"
                )

    # ── Threshold (T-type markets) ────────────────────────────────────────────
    if direction_meta in ("ABOVE", "BELOW"):
        # "above" markets: floor_strike holds the threshold
        # "below" markets: cap_strike holds the threshold
        kx_threshold = (
            meta.get("floor_strike") if direction_meta == "ABOVE"
            else meta.get("cap_strike")
        )
        our_thresh = audit.get("threshold_f")
        # Guard: Kalshi API sometimes returns cap_strike as a near-zero float
        # (bug observed 2026-06-13 for "less" markets — value like 7.7e-05 instead of 77)
        if (kx_threshold is not None and float(kx_threshold) >= 1.0
                and our_thresh is not None):
            if abs(float(kx_threshold) - float(our_thresh)) > 0.5:
                structural_mismatches.append(
                    f"THRESHOLD_MISMATCH: parser={our_thresh}, meta={kx_threshold}"
                )

    all_mismatches = structural_mismatches + info_notes
    matches = len(structural_mismatches) == 0
    details = json.dumps(all_mismatches) if all_mismatches else None
    return matches, direction_meta, details


def get_verified_contract_semantics(
    ticker: str,
    market_metadata: Optional[Dict[str, Any]] = None,
) -> ContractSemantics:
    """
    Build verified ContractSemantics from a Kalshi ticker + live market dict.

    market_metadata is the Kalshi REST API market object returned by
    get_weather_markets() and contains strike_type, floor_strike, cap_strike.

    Verification rules:
      BAND     (HIGH_BAND from ticker)   — direction always BAND     → verified=True
      HOURLY   (HOURLY_ABOVE from ticker)— direction always ABOVE    → verified=True
      THRESHOLD(HIGH_ABOVE from ticker)  — direction from strike_type:
          strike_type present → verified=True
          strike_type absent  → verified=False (DIRECTION_UNKNOWN_NO_METADATA)
      Structural mismatch (metadata direction contradicts parser type) → verified=False
      Parse failure or unknown city                                    → verified=False
    """
    _ticker = ticker.upper().strip()
    parsed = _parse_ticker(_ticker)
    if parsed is None:
        return ContractSemantics(
            ticker=ticker, canonical_city=None, market_type=None, contract_type=None,
            direction=None, threshold=None, floor_strike=None, cap_strike=None,
            settlement_date=None, settlement_hour=None,
            verified=False, failure_reason="PARSE_FAILED",
        )

    city = parsed.get("city")
    if not city or city not in _CITY_MAP:
        return ContractSemantics(
            ticker=ticker, canonical_city=city, market_type=parsed.get("type"),
            contract_type=None, direction=None, threshold=None,
            floor_strike=None, cap_strike=None,
            settlement_date=None, settlement_hour=None,
            verified=False, failure_reason=f"UNKNOWN_CITY:{city}",
        )

    mtype = parsed.get("type")         # HIGH_BAND | HIGH_ABOVE | HOURLY_ABOVE
    date_str = parsed.get("date_str")
    settlement_date: Optional[str] = None
    if date_str:
        try:
            settlement_date = datetime.strptime(date_str, "%y%b%d").strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Map internal type to canonical contract_type
    if mtype == "HIGH_BAND":
        contract_type = "BAND"
    elif mtype == "HOURLY_ABOVE":
        contract_type = "HOURLY"
    else:
        contract_type = "THRESHOLD"

    strike_type_raw = ((market_metadata or {}).get("strike_type") or "").lower()
    direction_meta = _STRIKE_TYPE_TO_DIRECTION.get(strike_type_raw) if strike_type_raw else None

    if mtype == "HIGH_BAND":
        # Structural mismatch: metadata says ABOVE/BELOW but parser says BAND
        if direction_meta and direction_meta != "BAND":
            return ContractSemantics(
                ticker=ticker, canonical_city=city, market_type=mtype,
                contract_type=contract_type, direction=None,
                threshold=None, floor_strike=parsed.get("lower"), cap_strike=parsed.get("upper"),
                settlement_date=settlement_date, settlement_hour=None,
                verified=False,
                failure_reason=f"STRUCTURAL_MISMATCH:parser=BAND,meta={direction_meta}",
            )
        return ContractSemantics(
            ticker=ticker, canonical_city=city, market_type=mtype,
            contract_type=contract_type, direction="BAND",
            threshold=None, floor_strike=parsed.get("lower"), cap_strike=parsed.get("upper"),
            settlement_date=settlement_date, settlement_hour=None,
            verified=True, failure_reason=None,
        )

    elif mtype == "HOURLY_ABOVE":
        # Structural mismatch: metadata says BAND but parser says HOURLY
        if direction_meta == "BAND":
            return ContractSemantics(
                ticker=ticker, canonical_city=city, market_type=mtype,
                contract_type=contract_type, direction=None,
                threshold=parsed.get("threshold"), floor_strike=None, cap_strike=None,
                settlement_date=settlement_date, settlement_hour=parsed.get("hour"),
                verified=False,
                failure_reason="STRUCTURAL_MISMATCH:parser=HOURLY,meta=BAND",
            )
        return ContractSemantics(
            ticker=ticker, canonical_city=city, market_type=mtype,
            contract_type=contract_type, direction="ABOVE",
            threshold=parsed.get("threshold"), floor_strike=None, cap_strike=None,
            settlement_date=settlement_date, settlement_hour=parsed.get("hour"),
            verified=True, failure_reason=None,
        )

    elif mtype == "HIGH_ABOVE":
        # THRESHOLD: direction MUST come from metadata strike_type
        if not strike_type_raw:
            return ContractSemantics(
                ticker=ticker, canonical_city=city, market_type=mtype,
                contract_type=contract_type, direction=None,
                threshold=parsed.get("threshold"), floor_strike=None, cap_strike=None,
                settlement_date=settlement_date, settlement_hour=None,
                verified=False, failure_reason="DIRECTION_UNKNOWN_NO_METADATA",
            )
        if direction_meta is None:
            return ContractSemantics(
                ticker=ticker, canonical_city=city, market_type=mtype,
                contract_type=contract_type, direction=None,
                threshold=parsed.get("threshold"), floor_strike=None, cap_strike=None,
                settlement_date=settlement_date, settlement_hour=None,
                verified=False, failure_reason=f"UNKNOWN_STRIKE_TYPE:{strike_type_raw}",
            )
        if direction_meta == "BAND":
            return ContractSemantics(
                ticker=ticker, canonical_city=city, market_type=mtype,
                contract_type=contract_type, direction=None,
                threshold=parsed.get("threshold"), floor_strike=None, cap_strike=None,
                settlement_date=settlement_date, settlement_hour=None,
                verified=False,
                failure_reason="STRUCTURAL_MISMATCH:parser=THRESHOLD,meta=BAND",
            )
        # direction_meta is ABOVE or BELOW — verified
        return ContractSemantics(
            ticker=ticker, canonical_city=city, market_type=mtype,
            contract_type=contract_type, direction=direction_meta,
            threshold=parsed.get("threshold"), floor_strike=None, cap_strike=None,
            settlement_date=settlement_date, settlement_hour=None,
            verified=True, failure_reason=None,
        )

    return ContractSemantics(
        ticker=ticker, canonical_city=city, market_type=mtype,
        contract_type=None, direction=None, threshold=None,
        floor_strike=None, cap_strike=None,
        settlement_date=settlement_date, settlement_hour=None,
        verified=False, failure_reason=f"UNKNOWN_MARKET_TYPE:{mtype}",
    )


async def fetch_and_audit_metadata(ticker: str, client, db) -> Dict:
    """
    Fetch Kalshi market metadata, compare to parser interpretation, and
    persist the combined result to contract_semantics.

    Fail-closed logic:
      - If parser_matches_metadata is False: unsupported=True → market will be skipped in index.py
      - If metadata 404 (market expired): unsupported=False, parser_matches_metadata=None
      - If direction is BELOW: direction_meta stored so index.py can invert p_yes

    Returns the audit dict (same structure as audit_ticker, plus metadata fields).
    """
    audit = audit_ticker(ticker)
    meta: Optional[Dict] = None

    try:
        meta = await client.get_market(ticker)
    except Exception as e:
        logger.warning("METADATA_FETCH_FAILED [%s]: %s", ticker, e)

    parser_matches: Optional[bool] = None
    direction_meta: Optional[str] = None
    mismatch_details: Optional[str] = None
    unsupported = False

    if meta is not None:
        parser_matches, direction_meta, mismatch_details = _compare_parser_to_metadata(audit, meta)
        if not parser_matches:
            unsupported = True
            logger.warning(
                "PARSER_MISMATCH [%s]: %s — marking unsupported",
                ticker, mismatch_details
            )
    else:
        logger.debug("METADATA_UNAVAILABLE [%s]: market may be expired — allowing trade", ticker)

    now = datetime.now(timezone.utc).isoformat()
    try:
        db.upsert_contract_semantics(
            ticker                  = ticker,
            market_type             = audit.get("market_type"),
            city                    = audit.get("city"),
            settle_date             = audit.get("settle_date"),
            hour_local              = audit.get("hour_local"),
            threshold_f             = audit.get("threshold_f"),
            direction               = audit.get("direction"),
            bucket_lower_f          = audit.get("bucket_lower_f"),
            bucket_upper_f          = audit.get("bucket_upper_f"),
            timezone_city           = audit.get("timezone_city"),
            temp_source             = audit.get("temp_source"),
            settlement_rule         = audit.get("settlement_rule"),
            audit_warnings          = json.dumps(audit.get("warnings", [])),
            kx_title                = (meta or {}).get("title"),
            kx_rules_primary        = (meta or {}).get("rules_primary"),
            kx_strike_type          = (meta or {}).get("strike_type"),
            kx_floor_strike         = (meta or {}).get("floor_strike"),
            kx_cap_strike           = (meta or {}).get("cap_strike"),
            kx_occurrence_datetime  = (meta or {}).get("occurrence_datetime"),
            parser_matches_metadata = parser_matches,
            unsupported             = unsupported,
            mismatch_details        = mismatch_details,
            direction_meta          = direction_meta,
            metadata_fetched_at     = now,
        )
    except Exception as e:
        logger.error("CONTRACT_AUDIT_META_STORE_FAILED [%s]: %s", ticker, e)

    return {
        **audit,
        "parser_matches_metadata": parser_matches,
        "unsupported":             unsupported,
        "direction_meta":          direction_meta,
        "mismatch_details":        mismatch_details,
    }
