import math
from typing import Dict, Any, Optional
from dataclasses import dataclass


@dataclass
class TradeIntent:
    ticker: str
    side: str
    price_cents: int
    target_qty: int
    expected_value: float
    kelly_fraction: float
    confidence: float
    scan_id: Optional[int] = None
    reason: str = ""


# Kalshi fee formula: ceil(0.07 * C * P * (1-P) * 100) cents
def _kalshi_fee_per_contract(price_cents: int) -> float:
    p = price_cents / 100.0
    return math.ceil(0.07 * p * (1.0 - p) * 100.0)


class DecisionEngine:
    """
    Pure math engine — no I/O, no DB calls.
    Accepts an optional BrainModel; falls back to conservative defaults when brain=None.

    Evaluates both YES and NO sides across the full price range.
    A trade fires when: EV_net > min_edge_cents AND Kelly > 0.
    """

    def __init__(
        self,
        brain=None,
        max_kelly_fraction: float = 0.15,
        kelly_multiplier: float = 1.0,
        min_edge_cents: float = 5.0,   # minimum net EV per contract to fire
        min_total_ev: float = 3.0,     # minimum total EV (cents) across the order
    ):
        self.brain = brain
        self.max_kelly_fraction = max_kelly_fraction
        self.kelly_multiplier = kelly_multiplier
        self.min_edge_cents = min_edge_cents
        self.min_total_ev = min_total_ev

    def _kelly_qty(self, prob_win: float, price_cents: int, balance: float) -> tuple:
        """Returns (kelly_fraction, qty) for a given win probability and entry price."""
        if price_cents <= 0 or price_cents >= 100:
            return 0.0, 0
        fee = _kalshi_fee_per_contract(price_cents)
        # Notebook formula: net_odds = (100 - price - fee) / (price + fee)
        # Effective stake = price + fee, win = 100 - price - fee
        effective_stake = price_cents + fee
        net_gain = 100 - effective_stake
        if effective_stake <= 0 or net_gain <= 0:
            return 0.0, 0
        net_odds = net_gain / effective_stake
        q = 1.0 - prob_win
        base_kelly = max(0.0, (net_odds * prob_win - q) / net_odds) if net_odds > 0 else 0.0
        final_kelly = min(base_kelly * self.kelly_multiplier, self.max_kelly_fraction)
        cost = effective_stake / 100.0
        qty = int((balance * final_kelly) / cost) if cost > 0 else 0
        return final_kelly, qty

    def evaluate(
        self,
        market: Dict[str, Any],
        scan_id: int,
        current_balance: float,
        env_mode: str,
        posterior: Optional[Dict[str, Any]] = None,
    ) -> Optional[TradeIntent]:
        ticker  = market.get("ticker")
        yes_ask = market.get("yes_ask")
        no_ask  = market.get("no_ask")
        yes_bid = market.get("yes_bid")

        if not ticker or yes_ask is None or no_ask is None:
            return None
        if yes_ask <= 0 or yes_ask >= 100 or no_ask <= 0 or no_ask >= 100:
            return None

        # ── Pure arbitrage: yes_ask + no_ask < 100 — risk-free profit ─────────
        if (yes_ask + no_ask) < 100:
            # Buy both YES and NO for guaranteed profit; here we just buy YES leg
            fee = _kalshi_fee_per_contract(yes_ask)
            ev_yes = (100 - yes_ask) - fee  # guaranteed payout minus entry minus fee
            return TradeIntent(
                ticker=ticker,
                side="yes",
                price_cents=yes_ask,
                target_qty=1,
                expected_value=ev_yes / 100.0,
                kelly_fraction=0.01,
                confidence=1.0,
                scan_id=scan_id,
                reason="ARBITRAGE_FOUND",
            )

        # ── Brain-informed EV-based rule (both sides, full price range) ────────
        posterior_data = posterior or {}

        if self.brain is not None:
            p_yes = self.brain.predict(market, posterior_data)
        else:
            # Conservative default: trust market mid-price with a small discount
            mid = (yes_ask + (100 - no_ask)) / 200.0  # average of two mid estimates
            p_yes = max(0.05, min(0.95, mid - 0.02))

        p_no = 1.0 - p_yes

        # ── Brier-penalized Kelly multiplier ─────────────────────────────────
        # When rolling_brier is high the model is poorly calibrated — shrink sizing.
        # effective_mult = base / (1 + 2 * brier). At brier=0.28: 0.25/1.56=0.16.
        # At brier=0.04 (good): 0.25/1.08=0.23. Floor at 0.05 to keep trades alive.
        rolling_brier = posterior_data.get("rolling_brier", 0.25)
        effective_mult = max(0.05, self.kelly_multiplier / (1.0 + 2.0 * rolling_brier))
        saved_mult = self.kelly_multiplier
        self.kelly_multiplier = effective_mult

        # ── Evaluate YES side ─────────────────────────────────────────────────
        fee_yes = _kalshi_fee_per_contract(yes_ask)
        ev_yes_per_contract = p_yes * 100.0 - yes_ask - fee_yes
        best_side, best_ev, best_price, best_prob = None, -999.0, 0, 0.0

        if ev_yes_per_contract >= self.min_edge_cents:
            kelly, qty = self._kelly_qty(p_yes, yes_ask, current_balance)
            if qty > 0 and ev_yes_per_contract * qty >= self.min_total_ev:
                best_side, best_ev, best_price, best_prob = "yes", ev_yes_per_contract, yes_ask, p_yes
                best_kelly, best_qty = kelly, qty

        # ── Evaluate NO side ──────────────────────────────────────────────────
        fee_no = _kalshi_fee_per_contract(no_ask)
        ev_no_per_contract = p_no * 100.0 - no_ask - fee_no

        if ev_no_per_contract >= self.min_edge_cents and ev_no_per_contract > best_ev:
            kelly, qty = self._kelly_qty(p_no, no_ask, current_balance)
            if qty > 0 and ev_no_per_contract * qty >= self.min_total_ev:
                best_side, best_ev, best_price, best_prob = "no", ev_no_per_contract, no_ask, p_no
                best_kelly, best_qty = kelly, qty

        self.kelly_multiplier = saved_mult

        if best_side is None:
            return None

        return TradeIntent(
            ticker=ticker,
            side=best_side,
            price_cents=best_price,
            target_qty=best_qty,
            expected_value=best_ev / 100.0,
            kelly_fraction=best_kelly,
            confidence=best_prob,
            scan_id=scan_id,
            reason="BRAIN_EV",
        )
