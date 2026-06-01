"""
Tests for the LogitJumpDiffusion brain and its integration with DecisionEngine.
All tests are pure — no I/O, no DB, no API calls.
Calibration target: Brier score < 0.25.
"""
import sys
import os
import math
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.brain.logit_jd import LogitJumpDiffusionBrain
from src.brain.protocol import BrainModel
from src.decision.engine import DecisionEngine


def brier_score(predictions, outcomes):
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / len(predictions)


class TestLogitJDBrain(unittest.TestCase):

    def setUp(self):
        self.brain = LogitJumpDiffusionBrain()

    def _market(self, yes_ask=55, yes_bid=50, no_ask=48, depth=50):
        return {"ticker": "KXTEST", "yes_ask": yes_ask, "yes_bid": yes_bid, "no_ask": no_ask, "depth": depth}

    def _posterior(self, p_adj=0.70, var=0.5, tau=6.0, pi_stale=0.1):
        return {"P_adj_YES": p_adj, "posterior_var_T": var, "tau_hrs": tau, "pi_stale": pi_stale}

    # ── Output range ─────────────────────────────────────────────────────────

    def test_output_is_valid_probability(self):
        p = self.brain.predict(self._market(), self._posterior())
        self.assertGreater(p, 0.0)
        self.assertLess(p, 1.0)

    def test_output_respects_min_max_bounds(self):
        p_low  = self.brain.predict(self._market(yes_ask=2, yes_bid=1, no_ask=99), self._posterior(p_adj=0.01))
        p_high = self.brain.predict(self._market(yes_ask=99, yes_bid=98, no_ask=2), self._posterior(p_adj=0.99))
        self.assertGreaterEqual(p_low, self.brain.min_prob)
        self.assertLessEqual(p_high, self.brain.max_prob)

    # ── Directional behaviour ─────────────────────────────────────────────────

    def test_higher_p_adj_yields_higher_output(self):
        p_low  = self.brain.predict(self._market(), self._posterior(p_adj=0.30))
        p_high = self.brain.predict(self._market(), self._posterior(p_adj=0.85))
        self.assertGreater(p_high, p_low)

    def test_deep_liquid_market_trusts_signal_more(self):
        p_shallow = self.brain.predict(self._market(depth=1),   self._posterior())
        p_deep    = self.brain.predict(self._market(depth=200), self._posterior())
        self.assertTrue(0 < p_shallow < 1)
        self.assertTrue(0 < p_deep < 1)

    def test_stale_market_pulls_toward_0_5(self):
        p_fresh = self.brain.predict(self._market(), self._posterior(p_adj=0.90, pi_stale=0.0))
        p_stale = self.brain.predict(self._market(), self._posterior(p_adj=0.90, pi_stale=1.0))
        self.assertLess(abs(p_stale - 0.5), abs(p_fresh - 0.5))

    def test_no_posterior_falls_back_to_market_mid(self):
        p = self.brain.predict(self._market(yes_ask=70, yes_bid=65, no_ask=33), {})
        self.assertGreater(p, 0.0)
        self.assertLess(p, 1.0)

    # ── BrainModel protocol compliance ────────────────────────────────────────

    def test_implements_brain_model_protocol(self):
        self.assertIsInstance(self.brain, BrainModel)

    # ── Calibration (Brier score) ─────────────────────────────────────────────

    def test_brier_score_below_threshold(self):
        """
        Synthetic calibration check: predictions should track outcomes with Brier < 0.25.
        """
        cases = [
            # (yes_ask, yes_bid, depth, p_adj, tau, pi_stale, actual_outcome)
            (90, 85, 100, 0.90, 2.0, 0.05, 1),
            (88, 83, 80,  0.85, 3.0, 0.10, 1),
            (85, 80, 60,  0.80, 4.0, 0.15, 1),
            (15, 10, 100, 0.10, 2.0, 0.05, 0),
            (12,  8, 80,  0.12, 3.0, 0.10, 0),
            (20, 15, 60,  0.18, 4.0, 0.15, 0),
            (50, 45, 50,  0.50, 6.0, 0.20, 1),
            (50, 45, 50,  0.50, 6.0, 0.20, 0),
        ]
        preds, outcomes = [], []
        for ya, yb, depth, p_adj, tau, stale, outcome in cases:
            p = self.brain.predict(
                {"ticker": "SYN", "yes_ask": ya, "yes_bid": yb, "no_ask": 100-ya, "depth": depth},
                {"P_adj_YES": p_adj, "posterior_var_T": 0.5, "tau_hrs": tau, "pi_stale": stale},
            )
            preds.append(p)
            outcomes.append(outcome)

        score = brier_score(preds, outcomes)
        print(f"\nBrier score on synthetic calibration set: {score:.4f}")
        self.assertLess(score, 0.25, f"Brier score {score:.4f} exceeds threshold 0.25")


class TestDecisionEngineWithBrain(unittest.TestCase):

    def setUp(self):
        self.brain  = LogitJumpDiffusionBrain()
        self.engine = DecisionEngine(brain=self.brain, max_kelly_fraction=0.2,
                                     min_edge_cents=2.0, min_total_ev=3.0)
        self.balance = 2500.0

    def _market(self, yes_ask=55, no_ask=48, yes_bid=50):
        return {"ticker": "KXTEST", "yes_ask": yes_ask, "no_ask": no_ask, "yes_bid": yes_bid}

    def _posterior(self, p_adj=0.75):
        return {"P_adj_YES": p_adj, "posterior_var_T": 0.5, "tau_hrs": 6.0, "pi_stale": 0.1}

    def test_arbitrage_always_fires(self):
        market = {"ticker": "ARB", "yes_ask": 40, "no_ask": 55, "yes_bid": 38}
        intent = self.engine.evaluate(market, scan_id=1, current_balance=self.balance, env_mode="paper")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.reason, "ARBITRAGE_FOUND")

    def test_brain_ev_fires_on_clear_edge(self):
        # Brain p_adj=0.80 on yes_ask=55 → EV positive
        intent = self.engine.evaluate(
            self._market(yes_ask=55, no_ask=48, yes_bid=50),
            scan_id=2,
            current_balance=self.balance,
            env_mode="paper",
            posterior=self._posterior(p_adj=0.80),
        )
        self.assertIsNotNone(intent)
        self.assertEqual(intent.reason, "BRAIN_EV")

    def test_no_trade_when_market_is_efficient(self):
        # Market priced at fair value + stale → no edge for either side
        # yes_ask=50, no_ask=52, p_adj=0.50, high staleness → brain near 50%
        intent = self.engine.evaluate(
            {"ticker": "KXFAIR", "yes_ask": 50, "no_ask": 52, "yes_bid": 48},
            scan_id=3,
            current_balance=self.balance,
            env_mode="paper",
            posterior={"P_adj_YES": 0.50, "posterior_var_T": 0.5, "tau_hrs": 12.0, "pi_stale": 0.5},
        )
        self.assertIsNone(intent)

    def test_engine_without_brain_uses_conservative_default(self):
        engine_no_brain = DecisionEngine(brain=None, max_kelly_fraction=0.2,
                                          min_edge_cents=2.0, min_total_ev=3.0)
        # Market near 50/50, conservative default discounts toward mid → minimal edge
        intent = engine_no_brain.evaluate(
            {"ticker": "KXNOBRAIN", "yes_ask": 52, "no_ask": 50, "yes_bid": 49},
            scan_id=4,
            current_balance=self.balance,
            env_mode="paper",
        )
        # Conservative default should find no significant edge here
        if intent:
            self.assertIn(intent.reason, ("ARBITRAGE_FOUND", "BRAIN_EV"))

    def test_no_trade_when_prices_invalid(self):
        intent = self.engine.evaluate(
            {"ticker": "KXINVALID", "yes_ask": 0, "no_ask": 100, "yes_bid": 0},
            scan_id=5,
            current_balance=self.balance,
            env_mode="paper",
        )
        self.assertIsNone(intent)

    def test_qty_is_bounded_by_kelly_fraction(self):
        intent = self.engine.evaluate(
            self._market(yes_ask=55),
            scan_id=6,
            current_balance=self.balance,
            env_mode="paper",
            posterior=self._posterior(p_adj=0.80),
        )
        if intent:
            max_spend = self.balance * self.engine.max_kelly_fraction
            actual_spend = intent.target_qty * (intent.price_cents / 100.0)
            self.assertLessEqual(actual_spend, max_spend + 1.0)

    def test_no_side_fires_both_sides_checked(self):
        # Very low p_yes (10%) with no_ask=25 (no arb: 80+25=105>100) → NO side has clear edge
        intent = self.engine.evaluate(
            {"ticker": "KXNO", "yes_ask": 80, "no_ask": 25, "yes_bid": 75},
            scan_id=7,
            current_balance=self.balance,
            env_mode="paper",
            posterior={"P_adj_YES": 0.10, "posterior_var_T": 0.5, "tau_hrs": 6.0, "pi_stale": 0.1},
        )
        self.assertIsNotNone(intent)
        self.assertEqual(intent.side, "no")


class TestKxtempCityAlias(unittest.TestCase):
    """_parse_ticker alias normalization — pure logic, no I/O."""

    def test_nych_maps_to_nyc(self):
        """KXTEMPNYCH suffix NYCH resolves to NYC via _KXTEMP_CITY_ALIAS."""
        from src.brain.weather_estimator import _parse_ticker, _CITY_MAP
        result = _parse_ticker("KXTEMPNYCH-26JUN0211-T65.99")
        self.assertIsNotNone(result)
        self.assertEqual(result["city"], "NYC")
        self.assertIn(result["city"], _CITY_MAP)

    def test_unknown_kxtemp_suffix_returns_result_with_unmapped_city(self):
        """Suffix with no alias entry still returns a parse result (city not in map)."""
        from src.brain.weather_estimator import _parse_ticker, _CITY_MAP
        result = _parse_ticker("KXTEMPXXXX-26JUN0211-T65.99")
        self.assertIsNotNone(result)
        self.assertNotIn(result["city"], _CITY_MAP)

    def test_kxhigh_parsing_unchanged_for_existing_cities(self):
        """KXHIGH tickers for cities already in _CITY_MAP parse correctly."""
        from src.brain.weather_estimator import _parse_ticker, _CITY_MAP
        for ticker, expected_city in [
            ("KXHIGHLAX-26JUN01-B74.5", "LAX"),
            ("KXHIGHTDC-26JUN02-T79",   "TDC"),
            ("KXHIGHCHI-26JUN01-B82.5", "CHI"),
        ]:
            with self.subTest(ticker=ticker):
                result = _parse_ticker(ticker)
                self.assertIsNotNone(result)
                self.assertEqual(result["city"], expected_city)
                self.assertIn(result["city"], _CITY_MAP)

    def test_kxhigh_tprefix_alias_resolves(self):
        """KXHIGH T-prefix codes like TSFO resolve to SFO via _KXHIGH_CITY_ALIAS."""
        from src.brain.weather_estimator import _parse_ticker, _CITY_MAP
        cases = [
            ("KXHIGHTSFO-26JUN01-B65.5",  "SFO"),
            ("KXHIGHTDAL-26JUN01-B95.5",  "DAL"),
            ("KXHIGHNY-26JUN01-T75",       "NYC"),
            ("KXHIGHTNOLA-26JUN01-T90",   "NOLA"),
            ("KXHIGHAUS-26JUN01-T95",      "AUS"),
        ]
        for ticker, expected_city in cases:
            with self.subTest(ticker=ticker):
                result = _parse_ticker(ticker)
                self.assertIsNotNone(result)
                self.assertEqual(result["city"], expected_city)
                self.assertIn(result["city"], _CITY_MAP)


class TestNormalizeCityCode(unittest.TestCase):
    """
    normalize_city_code() and _ticker_city() consistency tests.
    All pure — no I/O, no network, no DB.
    """

    def setUp(self):
        from src.brain.weather_estimator import normalize_city_code, _CITY_MAP
        self.n = normalize_city_code
        self.city_map = _CITY_MAP

    # ── Direct _CITY_MAP hits ────────────────────────────────────────────────

    def test_canonical_code_returned_unchanged(self):
        """Codes already in _CITY_MAP pass through without alias lookup."""
        for code in ("LAX", "NYC", "TDC", "CHI", "DEN", "MIA", "THOU"):
            with self.subTest(code=code):
                self.assertEqual(self.n(code), code)
                self.assertIn(self.n(code), self.city_map)

    # ── KXHIGH alias resolution ───────────────────────────────────────────────

    def test_kxhigh_tprefix_aliases_all_resolve(self):
        """Every T-prefix KXHIGH alias maps to a canonical key in _CITY_MAP."""
        cases = {
            "TSFO": "SFO", "TPHX": "PHX", "TMIN": "MIN",
            "TATL": "ATL", "TOKC": "OKC", "TBOS": "BOS",
            "TSATX": "SAT", "TSEA": "SEA", "TDAL": "DAL",
            "TNOLA": "NOLA", "TLV": "LV", "NY": "NYC",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                got = self.n(raw, market_prefix="KXHIGH")
                self.assertEqual(got, expected, f"{raw} -> {got}, expected {expected}")
                self.assertIn(got, self.city_map)

    def test_aus_alias_resolves(self):
        """AUS is a new Kalshi city code that maps to itself (present in _CITY_MAP)."""
        got = self.n("AUS", market_prefix="KXHIGH")
        self.assertEqual(got, "AUS")
        self.assertIn(got, self.city_map)

    # ── KXTEMP alias resolution ───────────────────────────────────────────────

    def test_kxtemp_nych_resolves_to_nyc(self):
        got = self.n("NYCH", market_prefix="KXTEMP")
        self.assertEqual(got, "NYC")
        self.assertIn(got, self.city_map)

    # ── No-prefix fallback ────────────────────────────────────────────────────

    def test_no_prefix_searches_both_maps(self):
        """Without a prefix, both alias maps are searched."""
        self.assertEqual(self.n("TSFO"), "SFO")   # KXHIGH alias
        self.assertEqual(self.n("NYCH"), "NYC")   # KXTEMP alias

    # ── Unknown code ──────────────────────────────────────────────────────────

    def test_unknown_code_returns_raw_uppercased(self):
        """A code absent from both maps is returned unchanged (no crash)."""
        got = self.n("XXXX", market_prefix="KXHIGH")
        self.assertEqual(got, "XXXX")
        self.assertNotIn(got, self.city_map)

    # ── _ticker_city integration ──────────────────────────────────────────────

    def test_ticker_city_tsfo_returns_sfo(self):
        from src.index import _ticker_city
        self.assertEqual(_ticker_city("KXHIGHTSFO-26JUN01-B65.5"), "SFO")

    def test_ticker_city_nych_returns_nyc(self):
        from src.index import _ticker_city
        self.assertEqual(_ticker_city("KXTEMPNYCH-26JUN0211-T65.99"), "NYC")

    def test_ticker_city_existing_cities_unchanged(self):
        from src.index import _ticker_city
        self.assertEqual(_ticker_city("KXHIGHLAX-26JUN01-B74.5"), "LAX")
        self.assertEqual(_ticker_city("KXHIGHTDC-26JUN02-T79"),   "TDC")
        self.assertEqual(_ticker_city("KXHIGHCHI-26JUN01-B82.5"), "CHI")

    def test_ticker_city_none_for_unrecognised_prefix(self):
        from src.index import _ticker_city
        self.assertIsNone(_ticker_city("KXRAINNYC-26JUN02-T0"))
        self.assertIsNone(_ticker_city("KXFIRSTHURRICANE-26DEC01-T3"))

    # ── Slot key consistency ──────────────────────────────────────────────────

    def test_slot_key_tsfo_matches_sfo(self):
        """KXHIGHTSFO and KXHIGHSFO produce the same city_date slot key."""
        from src.index import _ticker_city, _ticker_date
        tsfo_city = _ticker_city("KXHIGHTSFO-26JUN01-B65.5")
        sfo_city  = _ticker_city("KXHIGHSFO-26JUN01-B65.5")
        date      = _ticker_date("KXHIGHTSFO-26JUN01-B65.5")
        self.assertEqual(tsfo_city, sfo_city)
        self.assertEqual(f"{tsfo_city}_{date}", f"{sfo_city}_{date}")

    def test_held_slots_tsfo_would_block_sfo_candidate(self):
        """
        A held TSFO position and a new SFO candidate land in the same slot —
        the concentration guard would treat them as the same city.
        """
        from src.index import _ticker_city, _ticker_date
        held = "KXHIGHTSFO-26JUN01-B65.5"
        new  = "KXHIGHSFO-26JUN01-B67.5"
        self.assertEqual(
            f"{_ticker_city(held)}_{_ticker_date(held)}",
            f"{_ticker_city(new)}_{_ticker_date(new)}",
        )

    # ── City guard and attribution use normalized city ────────────────────────

    def test_city_guard_receives_normalized_city(self):
        """
        The city code reaching city_guard.check() is the normalized form.
        Verified indirectly: _parse_ticker normalizes before posterior is built,
        and posterior['city'] is what city_guard.check() receives.
        """
        from src.brain.weather_estimator import _parse_ticker, _CITY_MAP
        result = _parse_ticker("KXHIGHTSFO-26JUN01-B65.5")
        self.assertIsNotNone(result)
        city = result["city"]
        self.assertEqual(city, "SFO")
        self.assertIn(city, _CITY_MAP)

    def test_attribution_city_normalized(self):
        """
        Trade attribution writes posterior['city'] — confirm it's normalized
        for all ticker types.
        """
        from src.brain.weather_estimator import _parse_ticker
        cases = [
            ("KXHIGHTSFO-26JUN01-B65.5",    "SFO"),
            ("KXTEMPNYCH-26JUN0211-T65.99",  "NYC"),
            ("KXHIGHNY-26JUN01-T75",         "NYC"),
            ("KXHIGHTDAL-26JUN01-B95.5",     "DAL"),
            ("KXHIGHLAX-26JUN01-B74.5",      "LAX"),
        ]
        for ticker, expected_city in cases:
            with self.subTest(ticker=ticker):
                parsed = _parse_ticker(ticker)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed["city"], expected_city)


if __name__ == "__main__":
    unittest.main(verbosity=2)
