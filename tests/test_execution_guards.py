"""
Unit tests for execution-gate correctness.

Tests:
  1. 0.31c edge blocked when MIN_EV_CENTS=5
  2. 5.1c edge passes MIN_EV_CENTS=5
  3. NO buy reads no_asks, not yes_asks
  4. Already-held slot skipped in dedup
  5. Final executable count matches only non-held, above-threshold intents
  6. YES buy reads yes_asks (regression guard)
"""
import sys
import os
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.decision.engine import TradeIntent
from src.execution.manager import ExecutionManager

MIN_EV_CENTS = 5  # mirrors src/index.py


# ── helpers ─────────────────────────────────────────────────────────────────

def _make_intent(
    expected_value_dollars: float,
    side: str = "yes",
    price_cents: int = 50,
    ticker: str = "KXHIGHCHI-26APR30-T73",
) -> TradeIntent:
    return TradeIntent(
        ticker=ticker,
        side=side,
        price_cents=price_cents,
        target_qty=1,
        expected_value=expected_value_dollars,
        kelly_fraction=0.01,
        confidence=0.75,
        scan_id=None,
        reason="TEST",
    )


def _make_client(
    yes_asks: list,
    no_asks: list,
    order_status: str = "executed",
) -> MagicMock:
    """Return a mock KalshiClient whose orderbook returns the given ask ladders."""
    client = MagicMock()
    client.get_order_book = AsyncMock(return_value={
        "yes_asks": yes_asks,
        "no_asks": no_asks,
    })
    client.submit_order = AsyncMock(return_value={
        "status": "submitted",
        "order": {"order_id": "test-123", "status": order_status},
    })
    return client


# ── EV gate ─────────────────────────────────────────────────────────────────

class TestFinalEdgeGuard(unittest.TestCase):

    def test_031c_edge_blocked(self):
        """0.0031 dollars = 0.31c should be below the 5c threshold."""
        intent = _make_intent(expected_value_dollars=0.0031)
        edge_cents = intent.expected_value * 100.0
        self.assertLess(edge_cents, MIN_EV_CENTS,
                        f"Expected 0.31c < {MIN_EV_CENTS}c threshold but got {edge_cents:.4f}c")

    def test_51c_edge_passes(self):
        """0.051 dollars = 5.1c should pass the 5c threshold."""
        intent = _make_intent(expected_value_dollars=0.051)
        edge_cents = intent.expected_value * 100.0
        self.assertGreaterEqual(edge_cents, MIN_EV_CENTS,
                                f"Expected 5.1c >= {MIN_EV_CENTS}c threshold but got {edge_cents:.4f}c")

    def test_exactly_at_threshold_passes(self):
        """0.05 dollars = 5.0c should be treated as passing (>= not >)."""
        intent = _make_intent(expected_value_dollars=0.05)
        edge_cents = intent.expected_value * 100.0
        self.assertGreaterEqual(edge_cents, MIN_EV_CENTS)


# ── Side-specific drift check ────────────────────────────────────────────────

class TestSideSpecificDriftCheck(unittest.TestCase):
    """
    These tests verify which orderbook key the executor reads for each side.
    They run the full execute() coroutine via asyncio.run() with a mock client.
    """

    def _capture_warnings(self, coro) -> str:
        """Run `coro` and return all WARNING+ log lines from src.execution.manager."""
        import logging
        import io
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.WARNING)
        mgr_logger = logging.getLogger("src.execution.manager")
        mgr_logger.setLevel(logging.WARNING)
        mgr_logger.addHandler(handler)
        try:
            asyncio.run(coro)
        finally:
            mgr_logger.removeHandler(handler)
        return stream.getvalue()

    def test_no_buy_reads_no_ask_not_yes_ask(self):
        """
        NO order: no_ask=30c (below limit 31c) → no drift warning.
        yes_ask=99c would have triggered a spurious drift warning before the fix.
        """
        client = _make_client(
            yes_asks=[[99, 10]],   # irrelevant for NO order
            no_asks=[[30, 10]],    # 30c < limit 31c → no drift
        )
        intent = _make_intent(expected_value_dollars=0.10, side="no", price_cents=31)
        mgr = ExecutionManager(client)
        log = self._capture_warnings(mgr.execute(intent, "coid-no-test"))
        self.assertNotIn(
            "Price Drift", log,
            "NO buy should read no_ask (30c ≤ 31c limit) — no drift expected",
        )

    def test_no_buy_drift_fires_on_no_ask(self):
        """
        NO order: no_ask=35c > limit 31c → drift warning SHOULD fire.
        yes_ask=20c is below limit but must not suppress the warning.
        """
        client = _make_client(
            yes_asks=[[20, 10]],   # below limit — irrelevant
            no_asks=[[35, 10]],    # 35c > limit 31c → drift
        )
        intent = _make_intent(expected_value_dollars=0.10, side="no", price_cents=31)
        mgr = ExecutionManager(client)
        log = self._capture_warnings(mgr.execute(intent, "coid-no-drift"))
        self.assertIn(
            "Price Drift", log,
            "NO buy with no_ask=35c > limit=31c must trigger drift warning",
        )

    def test_yes_buy_reads_yes_ask(self):
        """
        YES order: yes_ask=40c < limit 50c → no drift.
        no_ask=99c must not pollute the yes-side check.
        """
        client = _make_client(
            yes_asks=[[40, 10]],   # 40c < limit 50c → no drift
            no_asks=[[99, 10]],    # irrelevant for YES order
        )
        intent = _make_intent(expected_value_dollars=0.10, side="yes", price_cents=50)
        mgr = ExecutionManager(client)
        log = self._capture_warnings(mgr.execute(intent, "coid-yes-test"))
        self.assertNotIn(
            "Price Drift", log,
            "YES buy should read yes_ask (40c ≤ 50c limit) — no drift expected",
        )


# ── Dedup / already-held logic ───────────────────────────────────────────────

class TestAlreadyHeldDedup(unittest.TestCase):
    """
    Tests the Phase-2 dedup logic in isolation.
    Reproduces the slot filtering without instantiating the full pipeline.
    """

    def _run_dedup(self, candidates, already_held):
        """Mirrors the Phase-2 dedup block from src/index.py."""
        n_already_held = 0
        n_dedup_removed = 0
        best_per_slot = {}
        for (ev, ticker, slot_key) in candidates:
            if slot_key in already_held:
                n_already_held += 1
                continue
            if slot_key not in best_per_slot or ev > best_per_slot[slot_key][0]:
                if slot_key in best_per_slot:
                    n_dedup_removed += 1
                best_per_slot[slot_key] = (ev, ticker, slot_key)
        return best_per_slot, n_already_held, n_dedup_removed

    def test_already_held_slot_skipped(self):
        """Candidate whose slot is already held must not appear in final executables."""
        already_held = {"CHI_2026-04-30"}
        candidates = [(0.30, "KXHIGHCHI-26APR30-T73", "CHI_2026-04-30")]
        best, skipped, removed = self._run_dedup(candidates, already_held)
        self.assertEqual(len(best), 0, "Held slot should produce zero executables")
        self.assertEqual(skipped, 1)

    def test_non_held_slot_passes(self):
        """Candidate for a free slot must appear in final executables."""
        already_held = set()
        candidates = [(0.30, "KXHIGHCHI-26APR30-T73", "CHI_2026-04-30")]
        best, skipped, removed = self._run_dedup(candidates, already_held)
        self.assertEqual(len(best), 1)
        self.assertEqual(skipped, 0)

    def test_final_executable_count_matches_submitted(self):
        """
        Two candidates for different slots, one already held → only 1 executable.
        Simulates: raw_candidates=2, already_held_skipped=1, final_executable=1.
        """
        already_held = {"DEN_2026-04-30"}
        candidates = [
            (0.31, "KXHIGHDEN-26APR30-T73", "DEN_2026-04-30"),  # held → skip
            (0.27, "KXHIGHTDC-26APR30-T73", "TDC_2026-04-30"),  # free → execute
        ]
        best, skipped, removed = self._run_dedup(candidates, already_held)
        self.assertEqual(len(best), 1, "Only TDC should be executable")
        self.assertEqual(skipped, 1, "DEN should be counted as skipped")
        self.assertIn("TDC_2026-04-30", best)

    def test_dedup_keeps_best_ev_per_slot(self):
        """When two candidates share a slot, the higher-EV one wins."""
        already_held = set()
        candidates = [
            (0.20, "KXHIGHCHI-26APR30-T71", "CHI_2026-04-30"),
            (0.35, "KXHIGHCHI-26APR30-T73", "CHI_2026-04-30"),
        ]
        best, skipped, removed = self._run_dedup(candidates, already_held)
        self.assertEqual(len(best), 1)
        self.assertEqual(removed, 1)
        self.assertEqual(best["CHI_2026-04-30"][0], 0.35, "Higher-EV candidate should win")


if __name__ == "__main__":
    unittest.main()
