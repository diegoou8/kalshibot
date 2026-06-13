"""
Tests for src/risk/contract_audit.py — ticker semantics parsing.

Covers:
  - KXHIGH B-type (band): direction, bucket boundaries, settlement rule
  - KXHIGH T-type (threshold/above): direction, settlement rule
  - KXTEMP hourly T-type: hour parsing, direction
  - City alias normalization (NY→NYC, TSFO→SFO, etc.)
  - Unknown prefix rejection
  - Unknown city code flagged in warnings
  - Temp range sanity (out-of-range flagged)
  - Hour range sanity (KXTEMP hour > 23 flagged)
  - Settlement rule answers the six open questions from the Jun-13 post-mortem
"""
import pytest
from src.risk.contract_audit import audit_ticker


# ── KXHIGH Band (B-type) ─────────────────────────────────────────────────────

def test_kxhigh_band_market_type():
    r = audit_ticker("KXHIGHCHI-26APR21-B72.5")
    assert r["market_type"] == "HIGH_BAND"

def test_kxhigh_band_direction_is_band():
    r = audit_ticker("KXHIGHCHI-26APR21-B72.5")
    assert r["direction"] == "BAND"

def test_kxhigh_band_bucket_lower():
    # B72.5: midpoint 72.5 → floor(72.5) = 72 — Kalshi floor_strike
    r = audit_ticker("KXHIGHCHI-26APR21-B72.5")
    assert r["bucket_lower_f"] == pytest.approx(72.0)

def test_kxhigh_band_bucket_upper():
    # B72.5: midpoint 72.5 → ceil(72.5) = 73 — Kalshi cap_strike
    r = audit_ticker("KXHIGHCHI-26APR21-B72.5")
    assert r["bucket_upper_f"] == pytest.approx(73.0)

def test_kxhigh_band_settlement_rule_contains_range():
    r = audit_ticker("KXHIGHCHI-26APR21-B72.5")
    assert "[72.0, 73.0)" in r["settlement_rule"], \
        "Settlement rule must specify the half-open interval [floor(mid), ceil(mid))"

def test_kxhigh_band_threshold_f_is_none():
    # Band tickers have no single threshold
    r = audit_ticker("KXHIGHCHI-26APR21-B72.5")
    assert r["threshold_f"] is None

def test_kxhigh_band_temp_source_is_daily_max():
    r = audit_ticker("KXHIGHCHI-26APR21-B72.5")
    assert "daily_max" in r["temp_source"]

def test_kxhigh_band_date_parsed():
    r = audit_ticker("KXHIGHCHI-26APR21-B72.5")
    assert r["settle_date"] == "2026-04-21"

def test_kxhigh_band_hour_is_none():
    r = audit_ticker("KXHIGHCHI-26APR21-B72.5")
    assert r["hour_local"] is None

def test_kxhigh_band_valid():
    r = audit_ticker("KXHIGHCHI-26APR21-B72.5")
    assert r["valid"] is True


# ── KXHIGH Threshold (T-type / above) ────────────────────────────────────────

def test_kxhigh_threshold_market_type():
    r = audit_ticker("KXHIGHCHI-26APR21-T73")
    assert r["market_type"] == "HIGH_ABOVE"

def test_kxhigh_threshold_direction_is_above():
    r = audit_ticker("KXHIGHCHI-26APR21-T73")
    assert r["direction"] == "ABOVE"

def test_kxhigh_threshold_value():
    r = audit_ticker("KXHIGHCHI-26APR21-T73")
    assert r["threshold_f"] == pytest.approx(73.0)

def test_kxhigh_threshold_settlement_rule_contains_gte():
    r = audit_ticker("KXHIGHCHI-26APR21-T73")
    assert ">= 73.0" in r["settlement_rule"], \
        "T-type settlement rule must say YES if temp >= threshold"

def test_kxhigh_threshold_no_bucket():
    r = audit_ticker("KXHIGHCHI-26APR21-T73")
    assert r["bucket_lower_f"] is None
    assert r["bucket_upper_f"] is None

def test_kxhigh_threshold_temp_source_daily_max():
    r = audit_ticker("KXHIGHCHI-26APR21-T73")
    assert "daily_max" in r["temp_source"]

def test_kxhigh_threshold_valid():
    r = audit_ticker("KXHIGHCHI-26APR21-T73")
    assert r["valid"] is True


# ── KXTEMP Hourly (T-type) ────────────────────────────────────────────────────

def test_kxtemp_market_type():
    r = audit_ticker("KXTEMPNYCH-26APR2118-T68")
    assert r["market_type"] == "HOURLY_ABOVE"

def test_kxtemp_direction_is_above():
    r = audit_ticker("KXTEMPNYCH-26APR2118-T68")
    assert r["direction"] == "ABOVE"

def test_kxtemp_hour_parsed():
    r = audit_ticker("KXTEMPNYCH-26APR2118-T68")
    assert r["hour_local"] == 18

def test_kxtemp_threshold_parsed():
    r = audit_ticker("KXTEMPNYCH-26APR2118-T68")
    assert r["threshold_f"] == pytest.approx(68.0)

def test_kxtemp_settlement_rule_mentions_hour():
    r = audit_ticker("KXTEMPNYCH-26APR2118-T68")
    assert "18:00" in r["settlement_rule"]

def test_kxtemp_temp_source_is_hourly():
    r = audit_ticker("KXTEMPNYCH-26APR2118-T68")
    assert "hourly" in r["temp_source"]

def test_kxtemp_valid():
    r = audit_ticker("KXTEMPNYCH-26APR2118-T68")
    assert r["valid"] is True


# ── City alias normalization ──────────────────────────────────────────────────

def test_kxhigh_ny_alias_resolves_to_nyc():
    # KXHIGHNY-... should normalize to city=NYC
    r = audit_ticker("KXHIGHNY-26JUN07-B87.5")
    assert r["city"] == "NYC"

def test_kxhigh_tsfo_alias_resolves_to_sfo():
    r = audit_ticker("KXHIGHTSFO-26JUN07-T85")
    assert r["city"] == "SFO"

def test_kxhigh_tatl_alias_resolves_to_atl():
    r = audit_ticker("KXHIGHTATL-26JUN07-T95")
    assert r["city"] == "ATL"

def test_kxhigh_tsea_alias_resolves_to_sea():
    r = audit_ticker("KXHIGHTSEA-26JUN07-T80")
    assert r["city"] == "SEA"

def test_kxtemp_nych_alias_resolves_to_nyc():
    r = audit_ticker("KXTEMPNYCH-26APR2118-T68")
    assert r["city"] == "NYC"


# ── Timezone is populated for known cities ────────────────────────────────────

def test_timezone_chicago():
    r = audit_ticker("KXHIGHCHI-26APR21-T73")
    assert r["timezone_city"] == "America/Chicago"

def test_timezone_nyc():
    r = audit_ticker("KXHIGHNY-26JUN07-T90")
    assert r["timezone_city"] == "America/New_York"

def test_timezone_lax():
    r = audit_ticker("KXHIGHLAX-26JUN07-T95")
    assert r["timezone_city"] == "America/Los_Angeles"


# ── Unknown prefix ────────────────────────────────────────────────────────────

def test_unknown_prefix_invalid():
    r = audit_ticker("KXRANDOM-26APR21-T73")
    assert r["valid"] is False
    assert any("UNKNOWN_PREFIX" in w for w in r["warnings"])


# ── Unknown city ─────────────────────────────────────────────────────────────

def test_unknown_city_flagged_in_warnings():
    r = audit_ticker("KXHIGHXYZ-26APR21-T73")
    assert any("UNKNOWN_CITY" in w for w in r["warnings"])

def test_unknown_city_invalid():
    r = audit_ticker("KXHIGHXYZ-26APR21-T73")
    assert r["valid"] is False


# ── Temperature range sanity ─────────────────────────────────────────────────

def test_temp_out_of_range_above():
    r = audit_ticker("KXHIGHCHI-26APR21-T135")
    assert any("TEMP_OUT_OF_RANGE" in w for w in r["warnings"])

def test_temp_out_of_range_below():
    r = audit_ticker("KXHIGHCHI-26APR21-T-40")
    # This won't parse (T-type doesn't match negative) — should produce PARSE_FAILED
    assert r["valid"] is False

def test_normal_temp_no_range_warning():
    r = audit_ticker("KXHIGHCHI-26APR21-T85")
    assert not any("TEMP_OUT_OF_RANGE" in w for w in r["warnings"])


# ── KXTEMP hour range sanity ─────────────────────────────────────────────────

def test_kxtemp_valid_hour_23():
    r = audit_ticker("KXTEMPNYCH-26APR2123-T68")
    assert not any("HOUR_OUT_OF_RANGE" in w for w in r["warnings"])

def test_kxtemp_valid_hour_00():
    r = audit_ticker("KXTEMPNYCH-26APR2100-T68")
    assert not any("HOUR_OUT_OF_RANGE" in w for w in r["warnings"])


# ── NWS vs Open-Meteo discrepancy warning always present for KXHIGH ──────────

def test_kxhigh_always_has_nws_warning():
    for ticker in ("KXHIGHCHI-26APR21-T73", "KXHIGHCHI-26APR21-B72.5"):
        r = audit_ticker(ticker)
        assert any("MODEL_SOURCE_MISMATCH_RISK" in w for w in r["warnings"]), \
            f"NWS mismatch warning missing for {ticker}"

def test_kxtemp_no_nws_warning():
    r = audit_ticker("KXTEMPNYCH-26APR2118-T68")
    assert not any("MODEL_SOURCE_MISMATCH_RISK" in w for w in r["warnings"])


# ── Real traded tickers from DB (regression) ─────────────────────────────────

@pytest.mark.parametrize("ticker,expected_city,expected_type,expected_dir", [
    ("KXHIGHNY-26JUN07-B87.5",     "NYC", "HIGH_BAND",     "BAND"),
    ("KXHIGHTATL-26JUN07-B81.5",   "ATL", "HIGH_BAND",     "BAND"),
    ("KXHIGHTSEA-26JUN07-B63.5",   "SEA", "HIGH_BAND",     "BAND"),
    ("KXHIGHDEN-26JUN04-B78.5",    "DEN", "HIGH_BAND",     "BAND"),
    ("KXHIGHLAX-26JUN04-B75.5",    "LAX", "HIGH_BAND",     "BAND"),
    ("KXHIGHTDC-26JUN04-B88.5",    "TDC", "HIGH_BAND",     "BAND"),
    ("KXHIGHPHIL-26JUN04-B84.5",   "PHIL","HIGH_BAND",     "BAND"),
    ("KXHIGHTHOU-26JUN04-B94.5",   "THOU","HIGH_BAND",     "BAND"),
    ("KXHIGHCHI-26APR21-T73",      "CHI", "HIGH_ABOVE",    "ABOVE"),
    ("KXTEMPNYCH-26APR2118-T68",   "NYC", "HOURLY_ABOVE",  "ABOVE"),
])
def test_real_tickers(ticker, expected_city, expected_type, expected_dir):
    r = audit_ticker(ticker)
    assert r["market_type"] == expected_type,  f"{ticker}: market_type"
    assert r["city"]        == expected_city,  f"{ticker}: city"
    assert r["direction"]   == expected_dir,   f"{ticker}: direction"
    assert r["valid"] is True,                 f"{ticker}: valid=False, warnings={r['warnings']}"
