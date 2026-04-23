import numpy as np
from .models import EdgeMetrics, MicrostructureFeatures

class EdgeEngine:
    def compute(self, ticker: str, p_model: float, p_market: float, features: MicrostructureFeatures, uncertainty_penalty: float) -> EdgeMetrics:
        ev_raw = p_model - p_market
        
        # Adjusted EV penalizes high spread and model uncertainty
        spread_cost = features.spread / 2.0
        
        ev_adjusted = ev_raw - np.sign(ev_raw) * (spread_cost + uncertainty_penalty)
        
        if (ev_raw > 0 and ev_adjusted < 0) or (ev_raw < 0 and ev_adjusted > 0):
            ev_adjusted = 0.0
            
        return EdgeMetrics(
            ticker=ticker,
            EV_raw=ev_raw,
            EV_adjusted=ev_adjusted
        )
