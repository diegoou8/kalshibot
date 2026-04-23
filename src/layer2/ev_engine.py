import logging
import math
from typing import List, Tuple, Dict

logger = logging.getLogger(__name__)

class KalshiFeeModel:
    """
    Official formula-based fee calculator for Kalshi generic weather trading (Feb 2026).
    
    Formula: TotalFee = ceil(0.07 * C * P * (1 - P) * 100) cents.
    Where P = price / 100 (contract price in dollars), C = contracts.
    
    Note: This applies strictly to the broad Kalshi General Fee Schedule. 
    It does not cover maker rebates, taker-fee exceptions, or special index markets 
    unless provided via a specific fee_table override.
    """
    @staticmethod
    def get_total_fee_cents(price_cents: float, contracts: int) -> float:
        """
        Computes the total fee for an order in CENTS.
        price_cents: The weighted fill price in cents per contract.
        contracts: Order size Q.
        """
        if contracts <= 0:
            return 0.0
            
        p = price_cents / 100.0  # dollar price [0, 1]
        
        # 0.07 * C * P * (1 - P) ($USD)
        raw_fee_dollars = 0.07 * contracts * p * (1.0 - p)
        
        # Ceil of the cent-value (rounded to fix float 1.749999 precision artifacts)
        total_fee_cents = math.ceil(round(raw_fee_dollars * 100.0, 10))
        return float(total_fee_cents)

    @staticmethod
    def get_fee_per_contract(price_cents: float, contracts: int) -> float:
        """
        Returns average fee per contract (TotalFee / Q) in CENTS.
        Used for normalizing EV calculations to a per-contract basis.
        """
        total = KalshiFeeModel.get_total_fee_cents(price_cents, contracts)
        return total / contracts


class ExecutionEstimator:
    # ... logic identical to before ...
    def __init__(self, c1: float = 0.5, c2: float = 0.2):
        self.c1 = c1
        self.c2 = c2

    def calculate_fill_price(self, target_q: int, asks: List[Tuple[float, int]]) -> Tuple[float, float, str]:
        if not asks or target_q <= 0:
            return 0.0, 0.0, "INVALID_REQUEST"
        total_filled = 0
        total_cost = 0.0
        best_price = asks[0][0]
        for price, qty in asks:
            to_fill = min(qty, target_q - total_filled)
            total_cost += to_fill * price
            total_filled += to_fill
            if total_filled >= target_q:
                break
        if total_filled < target_q:
            return 0.0, 0.0, "DEPTH_INSUFFICIENT"
        avg_fill = total_cost / target_q
        slip_book = avg_fill - best_price
        return avg_fill, slip_book, "SUCCESS"

    def estimate_total_slippage(self, slip_book: float, spread: float, velocity: float) -> Tuple[float, float]:
        slip_buffer = self.c1 * spread + self.c2 * abs(velocity)
        return slip_buffer, max(slip_book, slip_buffer)


class EVEngine:
    """
    Core Engine for calculating Expected Value.
    All EV calculations are reported in CENTS PER CONTRACT.
    The order-level fee is converted using (TotalFee / Q) to ensure unit consistency.
    """
    def __init__(self, fee_model: KalshiFeeModel, estimator: ExecutionEstimator):
        self.fee_model = fee_model
        self.estimator = estimator

    def calculate_ev(self, side: str, target_q: int, p_adj: float, 
                     asks_ladder: List[Tuple[float, int]], spread: float, velocity: float) -> Dict:
        fill_price, slip_book, status = self.estimator.calculate_fill_price(target_q, asks_ladder)
        if status != "SUCCESS":
            return {"ev_cents": -999.0, "status": status}
            
        slip_buffer, slip_total = self.estimator.estimate_total_slippage(slip_book, spread, velocity)
        payout_per_contract = 100.0 * p_adj if side == "YES" else 100.0 * (1.0 - p_adj)
        
        # Fee per contract derived from order-level volume (TotalFee / Q)
        total_fee_cents = self.fee_model.get_total_fee_cents(fill_price, target_q)
        fee_per_contract = total_fee_cents / target_q
        
        best_ask = asks_ladder[0][0]
        
        # Unit (Cents/Contract): EV = Payout - (Entry + Fee_Avg + Slippage_Pred)
        ev_cents_per_contract = payout_per_contract - (best_ask + fee_per_contract + slip_total)
        
        return {
            "side": side,
            "ev_cents": ev_cents_per_contract,
            "total_ev_cents": ev_cents_per_contract * target_q,
            "payout_per_contract": payout_per_contract,
            "best_ask": best_ask,
            "fill_price": fill_price,
            "total_fee_cents": total_fee_cents,
            "fee_per_contract": fee_per_contract,
            "slip_book_cents": slip_book,
            "slip_buffer_cents": slip_buffer,
            "slip_total_cents": slip_total,
            "status": "SUCCESS"
        }
