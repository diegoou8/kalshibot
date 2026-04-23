import numpy as np
from .models import PosteriorState, IntervalProbabilities, MicrostructureFeatures, RiskMetrics

class RiskEngine:
    def compute(self, posterior: PosteriorState, probs: IntervalProbabilities, features: MicrostructureFeatures) -> RiskMetrics:
        # Entropy of discrete distribution
        prob_array = np.array(list(probs.probabilities.values()))
        prob_array = prob_array[prob_array > 0]
        entropy = -np.sum(prob_array * np.log(prob_array)) if len(prob_array) > 0 else 0.0
        
        spread_risk = features.spread
        depth_risk = 1.0 / max(features.total_depth, 1.0)
        
        confidence = 1.0 / (posterior.var_temp + 1e-6)
        
        # Uncertainty regime if entropy is extremely wide or variance is high
        regime = posterior.var_temp > 5.0 or entropy > 1.5
        
        return RiskMetrics(
            target_id=posterior.target_id,
            posterior_variance=posterior.var_temp,
            entropy=entropy,
            confidence_score=confidence,
            uncertainty_regime=regime,
            spread_risk=spread_risk,
            depth_risk=depth_risk
        )
