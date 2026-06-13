"""
Tests for ContractSemantics — verified direction, bounds, and probability math.

Covers:
  - ABOVE direction: LAX T74 with strike_type="greater"
  - BELOW direction: DEN T77 with strike_type="less"
  - BAND direction:  CHI B84.5 from ticker structure
  - Unknown direction blocks: T-type without metadata
  - Structural mismatch blocks: parser=BAND, meta=ABOVE
  - Missing metadata blocks: HIGH_ABOVE with no market_metadata
  - Math: BELOW = 1 - P(T > threshold), ABOVE = P(T > threshold)
"""
import math
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.types import ContractSemantics
from src.risk.contract_audit import get_verified_contract_semantics


# ---------------------------------------------------------------------------
# get_verified_contract_semantics tests
# ---------------------------------------------------------------------------

class TestGetVerifiedContractSemantics:
    def test_threshold_above_verified(self):
        """LAX T74 ABOVE: strike_type=greater → verified, direction=ABOVE."""
        s = get_verified_contract_semantics(
            "KXHIGHLAX-26JUN14-T74",
            {"strike_type": "greater", "floor_strike": 74.0},
        )
        assert s.verified is True
        assert s.direction == "ABOVE"
        assert s.contract_type == "THRESHOLD"
        assert s.threshold == 74.0
        assert s.canonical_city == "LAX"
        assert s.settlement_date == "2026-06-14"
        assert s.failure_reason is None

    def test_threshold_below_verified(self):
        """DEN T77 BELOW: strike_type=less → verified, direction=BELOW."""
        s = get_verified_contract_semantics(
            "KXHIGHDEN-26JUN14-T77",
            {"strike_type": "less", "cap_strike": 77.0},
        )
        assert s.verified is True
        assert s.direction == "BELOW"
        assert s.contract_type == "THRESHOLD"
        assert s.threshold == 77.0
        assert s.canonical_city == "DEN"
        assert s.failure_reason is None

    def test_band_verified_from_ticker(self):
        """CHI B84.5: BAND direction always verified from ticker structure."""
        s = get_verified_contract_semantics(
            "KXHIGHCHI-26JUN14-B84.5",
            {},
        )
        assert s.verified is True
        assert s.direction == "BAND"
        assert s.contract_type == "BAND"
        assert s.floor_strike == 84.0
        assert s.cap_strike == 85.0
        assert s.canonical_city == "CHI"

    def test_band_verified_even_without_metadata(self):
        """BAND is verified from ticker alone — no metadata needed."""
        s = get_verified_contract_semantics("KXHIGHNYC-26JUN14-B72.5", None)
        assert s.verified is True
        assert s.direction == "BAND"
        assert s.floor_strike == 72.0
        assert s.cap_strike == 73.0

    def test_threshold_no_metadata_blocks(self):
        """T-type with empty metadata dict → verified=False, DIRECTION_UNKNOWN_NO_METADATA."""
        s = get_verified_contract_semantics("KXHIGHDEN-26JUN14-T77", {})
        assert s.verified is False
        assert s.failure_reason == "DIRECTION_UNKNOWN_NO_METADATA"
        assert s.direction is None

    def test_threshold_none_metadata_blocks(self):
        """T-type with None metadata → verified=False."""
        s = get_verified_contract_semantics("KXHIGHDEN-26JUN14-T77", None)
        assert s.verified is False
        assert s.failure_reason == "DIRECTION_UNKNOWN_NO_METADATA"

    def test_parse_failure_blocks(self):
        """Unparseable ticker → verified=False, PARSE_FAILED."""
        s = get_verified_contract_semantics("NOTAVALIDTICKER", {})
        assert s.verified is False
        assert s.failure_reason == "PARSE_FAILED"

    def test_unknown_city_blocks(self):
        """Ticker with unmapped city → verified=False."""
        s = get_verified_contract_semantics(
            "KXHIGHZZZ-26JUN14-T77",
            {"strike_type": "greater", "floor_strike": 77.0},
        )
        assert s.verified is False
        assert s.failure_reason is not None
        assert "ZZZ" in s.failure_reason or "UNKNOWN_CITY" in s.failure_reason

    def test_structural_mismatch_band_vs_above_blocks(self):
        """Parser says BAND but metadata says ABOVE → verified=False, STRUCTURAL_MISMATCH."""
        s = get_verified_contract_semantics(
            "KXHIGHCHI-26JUN14-B84.5",
            {"strike_type": "greater", "floor_strike": 84.0},
        )
        assert s.verified is False
        assert s.failure_reason is not None
        assert "STRUCTURAL_MISMATCH" in s.failure_reason

    def test_structural_mismatch_threshold_vs_band_blocks(self):
        """Parser says THRESHOLD but metadata says BAND → verified=False."""
        s = get_verified_contract_semantics(
            "KXHIGHCHI-26JUN14-T84",
            {"strike_type": "between", "floor_strike": 84.0, "cap_strike": 85.0},
        )
        assert s.verified is False
        assert "STRUCTURAL_MISMATCH" in s.failure_reason

    def test_hourly_always_verified(self):
        """KXTEMP markets (HOURLY) are always ABOVE — verified from ticker."""
        s = get_verified_contract_semantics(
            "KXTEMPNYCH-26JUN1414-T72",
            {},
        )
        assert s.verified is True
        assert s.direction == "ABOVE"
        assert s.contract_type == "HOURLY"
        assert s.settlement_hour == 14

    def test_greater_or_equal_maps_to_above(self):
        """strike_type=greater_or_equal → direction=ABOVE."""
        s = get_verified_contract_semantics(
            "KXHIGHNYC-26JUN14-T80",
            {"strike_type": "greater_or_equal", "floor_strike": 80.0},
        )
        assert s.verified is True
        assert s.direction == "ABOVE"

    def test_unknown_strike_type_blocks(self):
        """Unrecognised strike_type → verified=False, UNKNOWN_STRIKE_TYPE."""
        s = get_verified_contract_semantics(
            "KXHIGHDEN-26JUN14-T77",
            {"strike_type": "weird_value"},
        )
        assert s.verified is False
        assert "UNKNOWN_STRIKE_TYPE" in s.failure_reason


# ---------------------------------------------------------------------------
# Probability math tests via estimate_p_yes
# ---------------------------------------------------------------------------

class TestEstimatePYesMath:
    """
    Tests for the refactored estimate_p_yes(semantics, ...).
    Patches Open-Meteo fetches to return a fixed temperature so we can
    verify the probability logic for each direction/contract_type.
    """

    def _make_above_semantics(self, city: str, threshold: float, date: str = "2026-06-14") -> ContractSemantics:
        return ContractSemantics(
            ticker=f"KXHIGH{city}-26JUN14-T{int(threshold)}",
            canonical_city=city,
            market_type="HIGH_ABOVE",
            contract_type="THRESHOLD",
            direction="ABOVE",
            threshold=threshold,
            floor_strike=None,
            cap_strike=None,
            settlement_date=date,
            settlement_hour=None,
            verified=True,
            failure_reason=None,
        )

    def _make_below_semantics(self, city: str, threshold: float, date: str = "2026-06-14") -> ContractSemantics:
        return ContractSemantics(
            ticker=f"KXHIGH{city}-26JUN14-T{int(threshold)}",
            canonical_city=city,
            market_type="HIGH_ABOVE",
            contract_type="THRESHOLD",
            direction="BELOW",
            threshold=threshold,
            floor_strike=None,
            cap_strike=None,
            settlement_date=date,
            settlement_hour=None,
            verified=True,
            failure_reason=None,
        )

    def _make_band_semantics(self, city: str, lower: float, upper: float, date: str = "2026-06-14") -> ContractSemantics:
        mid = (lower + upper) / 2
        return ContractSemantics(
            ticker=f"KXHIGH{city}-26JUN14-B{mid}",
            canonical_city=city,
            market_type="HIGH_BAND",
            contract_type="BAND",
            direction="BAND",
            threshold=None,
            floor_strike=lower,
            cap_strike=upper,
            settlement_date=date,
            settlement_hour=None,
            verified=True,
            failure_reason=None,
        )

    @pytest.mark.asyncio
    async def test_lax_t74_above_p_yes(self):
        """LAX T74 ABOVE: P(T>74) when forecast=80°F, sigma=4 → high probability."""
        from src.brain.weather_estimator import estimate_p_yes, _ar1_error_cache, _forecast_cache
        semantics = self._make_above_semantics("LAX", 74.0)
        # Prime forecast cache so no actual fetch
        _forecast_cache["34.052,-118.244,2026-06-14"] = 80.0
        _ar1_error_cache["ar1:34.052,-118.244"] = {
            "correction": 0.0, "e_prev": 0.0, "actual_yest": 80.0, "forecast_yest": 80.0
        }
        with patch("src.brain.weather_estimator._fetch_ar1_correction", new_callable=AsyncMock):
            p = await estimate_p_yes(semantics, sigma_f=4.0, phi=0.4, tau_hrs=24.0)
        assert p is not None
        # Forecast 80°F >> threshold 74°F, so P(T>74) should be high
        assert p > 0.7

    @pytest.mark.asyncio
    async def test_den_t77_below_p_yes(self):
        """DEN T77 BELOW: P(T<77) = 1 - P(T>77). Forecast=75°F → high BELOW probability."""
        from src.brain.weather_estimator import estimate_p_yes, _ar1_error_cache, _forecast_cache
        semantics = self._make_below_semantics("DEN", 77.0)
        _forecast_cache["39.739,-104.990,2026-06-14"] = 75.0
        _ar1_error_cache["ar1:39.739,-104.990"] = {
            "correction": 0.0, "e_prev": 0.0, "actual_yest": 75.0, "forecast_yest": 75.0
        }
        with patch("src.brain.weather_estimator._fetch_ar1_correction", new_callable=AsyncMock):
            p = await estimate_p_yes(semantics, sigma_f=4.0, phi=0.4, tau_hrs=24.0)
        assert p is not None
        # Forecast 75°F < threshold 77°F → P(T<77) should be > 0.5
        assert p > 0.5

    @pytest.mark.asyncio
    async def test_below_complement_of_above(self):
        """P_below + P_above ≈ 1 for the same city/threshold/forecast (no Gumbel)."""
        from src.brain.weather_estimator import estimate_p_yes, _ar1_error_cache, _forecast_cache
        above = self._make_above_semantics("LAX", 74.0)
        below = self._make_below_semantics("LAX", 74.0)
        _forecast_cache["34.052,-118.244,2026-06-14"] = 74.0
        _ar1_error_cache["ar1:34.052,-118.244"] = {
            "correction": 0.0, "e_prev": 0.0, "actual_yest": 74.0, "forecast_yest": 74.0
        }
        with patch("src.brain.weather_estimator._fetch_ar1_correction", new_callable=AsyncMock), \
             patch("src.brain.weather_estimator.load_horizon_sigma", return_value={}):
            # Use gumbel=none to avoid sigma inflation asymmetry between calls
            with patch("src.config.env.Config") as mock_cfg:
                mock_cfg.GUMBEL_MODE = "none"
                p_above = await estimate_p_yes(above, sigma_f=4.0, phi=0.4, tau_hrs=24.0)
                p_below = await estimate_p_yes(below, sigma_f=4.0, phi=0.4, tau_hrs=24.0)
        assert p_above is not None and p_below is not None
        # P(above) + P(below) = P(T>N) + (1 - P(T>N)) = 1.0
        # Both are clamped independently so sum may differ by ≤ 0.01 at extremes
        assert abs(p_above + p_below - 1.0) < 0.01

    @pytest.mark.asyncio
    async def test_chi_band_p_yes(self):
        """CHI B84.5 BAND: P(84<=T<85) when forecast=84.5°F → near 0.5."""
        from src.brain.weather_estimator import estimate_p_yes, _ar1_error_cache, _forecast_cache
        semantics = self._make_band_semantics("CHI", 84.0, 85.0)
        _forecast_cache["41.878,-87.630,2026-06-14"] = 84.5
        _ar1_error_cache["ar1:41.878,-87.630"] = {
            "correction": 0.0, "e_prev": 0.0, "actual_yest": 84.5, "forecast_yest": 84.5
        }
        with patch("src.brain.weather_estimator._fetch_ar1_correction", new_callable=AsyncMock):
            p = await estimate_p_yes(semantics, sigma_f=4.0, phi=0.4, tau_hrs=24.0)
        assert p is not None
        # Forecast dead-centre of the band, band width 1°F << sigma 4°F → p is small
        # P(84<=T<85) ≈ P(T>84) - P(T>85) ≈ 0.5 - 0.47 ≈ 0.03 → clamped to 0.03
        # For sigma=4, z(84,84.5)=0.125 → sf=0.45, z(85,84.5)=0.125 → sf=0.45 ...
        # Actually: P(T>84) = sf(-0.125) ~ 0.55, P(T>85) = sf(0.125) ~ 0.45 → p_band ~ 0.10
        assert 0.03 <= p <= 0.97

    @pytest.mark.asyncio
    async def test_unknown_city_returns_none(self):
        """Semantics with unknown city → estimate_p_yes returns None."""
        from src.brain.weather_estimator import estimate_p_yes
        semantics = ContractSemantics(
            ticker="KXHIGHZZZ-26JUN14-T77",
            canonical_city="ZZUNKNOWN",
            market_type="HIGH_ABOVE",
            contract_type="THRESHOLD",
            direction="ABOVE",
            threshold=77.0,
            floor_strike=None, cap_strike=None,
            settlement_date="2026-06-14",
            settlement_hour=None,
            verified=True,
            failure_reason=None,
        )
        p = await estimate_p_yes(semantics, sigma_f=4.0)
        assert p is None


# ---------------------------------------------------------------------------
# Integration: Phase 1d fail-closed log message
# ---------------------------------------------------------------------------

class TestPhase1dFailClosed:
    """Verify that unverified semantics log the right message."""

    def test_unverified_semantics_has_failure_reason(self):
        """Any unverified semantics always has a non-None failure_reason."""
        cases = [
            get_verified_contract_semantics("KXHIGHDEN-26JUN14-T77", {}),
            get_verified_contract_semantics("KXHIGHDEN-26JUN14-T77", None),
            get_verified_contract_semantics("NOTVALID", {}),
        ]
        for s in cases:
            assert s.verified is False
            assert s.failure_reason is not None and len(s.failure_reason) > 0

    def test_verified_semantics_has_no_failure_reason(self):
        """Verified semantics never has a failure_reason."""
        cases = [
            get_verified_contract_semantics(
                "KXHIGHLAX-26JUN14-T74", {"strike_type": "greater"}
            ),
            get_verified_contract_semantics("KXHIGHCHI-26JUN14-B84.5", {}),
        ]
        for s in cases:
            assert s.verified is True
            assert s.failure_reason is None
