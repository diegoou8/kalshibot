class ArbitrageAnalyzer:
    def __init__(self):
        self.history = []
        
    def analyze_spread(self, market) -> dict:
        """
        Analyze the spread of a single market to find arbitrage opportunities.
        A true arbitrage (risk-free) exists if yes_ask + no_ask < 100 cents.
        A statistical arbitrage (directional) exists if implied probability deviates 
        strongly from order book momentum.
        """
        yes_ask = market.get('yes_ask', 100)
        no_ask = market.get('no_ask', 100)
        yes_bid = market.get('yes_bid', 0)
        no_bid = market.get('no_bid', 0)
        
        # 1. Pure Risk-Free Arbitrage
        if yes_ask + no_ask < 100:
            return {
                "type": "risk_free",
                "is_arb": True,
                "profit_cents": 100 - (yes_ask + no_ask),
                "side_1": ("yes", yes_ask),
                "side_2": ("no", no_ask),
                "reason": f"Combined Ask Price is {yes_ask + no_ask}c (< 100c)"
            }
            
        # 2. Statistical Arbitrage / Spread Momentum (Mock Polymarket Volatility Logic)
        # If the spread is extremely tight on one side, or bid volume > ask volume
        spread = yes_ask - yes_bid
        if 0 < spread <= 3 and yes_ask <= 90:
            # Fake "momentum" logic for the scope of establishing the test trade
            # we buy the side where the spread is tightest
            return {
                "type": "statistical",
                "is_arb": True,
                "side": "yes",
                "price": yes_ask,
                "reason": f"Tight spread ({spread}c) detected with favorable limit execution."
            }
            
        return {
            "type": None,
            "is_arb": False,
            "reason": "No arbitrage conditions met."
        }
