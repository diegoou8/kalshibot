import sys
import os
import unittest
import math

# Ensure src is in the path
sys.path.append(os.path.join(os.getcwd(), 'src'))

from layer2.ev_engine import KalshiFeeModel, ExecutionEstimator, EVEngine

class TestLayer2Math(unittest.TestCase):
    def setUp(self):
        self.fee_model = KalshiFeeModel()
        self.estimator = ExecutionEstimator(c1=0.5, c2=0.2)
        self.ev_engine = EVEngine(self.fee_model, self.estimator)

    def test_official_fee_formula(self):
        print("\n--- Verifying Official Kalshi Fee Formula (Order-Level) ---")
        
        # Formula: ceil(0.07 * C * P * (1-P) * 100) -> total cents
        # 1 contract at 0.45: 0.07 * 1 * 0.45 * 0.55 = 0.017325. ceil(1.7325) = 2c.
        # 1 contract at 0.50: 0.07 * 1 * 0.50 * 0.50 = 0.0175.   ceil(1.75)   = 2c.
        # 100 contracts at 0.45: 0.07 * 100 * 0.45 * 0.55 = 1.7325. ceil(173.25) = 174c.
        # 100 contracts at 0.50: 0.07 * 100 * 0.50 * 0.50 = 1.75.   ceil(175)    = 175c.

        test_cases = [
            (45.0, 1, 2.0),
            (50.0, 1, 2.0),
            (45.0, 100, 174.0),
            (50.0, 100, 175.0)
        ]

        for p_cents, contracts, expected_total_cents in test_cases:
            fee_total = self.fee_model.get_total_fee_cents(p_cents, contracts)
            fee_per = self.fee_model.get_fee_per_contract(p_cents, contracts)
            print(f"Price: {p_cents/100:.2f}$ | Qty: {contracts:3d} | Total Fee: {fee_total:5.1f}c | Per: {fee_per:.2f}c")
            self.assertEqual(fee_total, expected_total_cents)

    def test_ev_with_quantity_aware_fees(self):
        print("\n--- Verifying Quantity-Aware EV Calculation ---")
        # 100 contracts at 0.45 (45.0 cents)
        # Prob = 0.55
        p_adj = 0.55
        target_q = 100
        velocity = 0.0
        spread = 0.0 
        ladder = [(45.0, 500)] # Infinite depth at 45.0
        
        result = self.ev_engine.calculate_ev("YES", target_q, p_adj, ladder, spread, velocity)
        
        # Payout = 100 * 0.55 = 55.0c per contract
        # Fee = 174c / 100 = 1.74c per contract
        # Slip = 0.0
        # EV = 55.0 - (45.0 + 1.74 + 0.0) = 8.26c
        
        print(f"Side: {result['side']} | Qty: {target_q}")
        print(f"Payout/C: {result['payout_per_contract']:.2f}c")
        print(f"Fee/C:    {result['fee_per_contract']:.2f}c")
        print(f"Net EV/C: {result['ev_cents']:.2f}c")
        
        self.assertAlmostEqual(result['ev_cents'], 8.26)

if __name__ == "__main__":
    print("========================================")
    print("Layer 2 Official Fee Formula Verification")
    print("========================================")
    unittest.main(argv=['first-arg-is-ignored'], exit=False)
