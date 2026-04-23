import numpy as np
from .models import MicrostructureFeatures

class ObservationModel:
    def __init__(self, a0=0.01, a1=1.0, a2=10.0, a3=0.05, a4=0.05):
        self.a0 = a0
        self.a1 = a1
        self.a2 = a2
        self.a3 = a3
        self.a4 = a4

    def compute_noise_variance(self, features: MicrostructureFeatures) -> float:
        """ Heteroskedastic observation noise based on market frictions """
        spread_term = self.a1 * (features.spread ** 2)
        depth_term = self.a2 / max(features.total_depth, 1.0)
        imbalance_term = self.a3 * (features.aggressor_imbalance ** 2)
        
        # Penalize observing price signal if it's far from a forecast update
        # because the price might just be off-cycle noise
        # If time_since_forecast < 300s (5m), penalty is low.
        time_penalty = 0.0
        if features.time_since_forecast > 60:
            time_penalty = self.a4 * (features.time_since_forecast / 300.0)

        sigma_obs_sq = self.a0 + spread_term + depth_term + imbalance_term + time_penalty
        return sigma_obs_sq

    def classify_move(self, features: MicrostructureFeatures, forecast_revision: float) -> str:
        if features.time_since_forecast < 10.0 and abs(forecast_revision) > 0.05:
            return "INFORMED_FORECAST_ALIGNED"
        elif features.spread > 0.10 and features.total_depth < 500:
            return "STALE_LIQUIDITY"
        else:
            return "OFF_CYCLE_NOISE"
