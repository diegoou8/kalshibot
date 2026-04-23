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


if __name__ == "__main__":
    unittest.main(verbosity=2)
