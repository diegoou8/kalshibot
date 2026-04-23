import numpy as np
from typing import Dict, List, Tuple
from .models import PosteriorState, IntervalProbabilities

class IntervalMapper:
    def map_probabilities(self, posterior: PosteriorState, intervals: Dict[str, Tuple[float, float]]) -> IntervalProbabilities:
        """
        Convert particles back to discrete probability states for every interval.
        """
        probs = {}
        for ticker, (L, U) in intervals.items():
            # Sum weights of particles bounded in the interval
            in_range = (posterior.particles >= L) & (posterior.particles < U)
            p = float(np.sum(posterior.weights[in_range]))
            probs[ticker] = p
            
        return IntervalProbabilities(target_id=posterior.target_id, probabilities=probs)
