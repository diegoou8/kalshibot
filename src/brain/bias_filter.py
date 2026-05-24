"""
Adaptive bias filter for forecast error correction.
Replaces fixed AR(1) phi=0.4 with an online Kalman-style gain.

b_t = b_{t-1} + K_t * (e_t - b_{t-1})

K_t is elevated when recent errors are sign-persistent (regime shift),
and suppressed when errors are mean-reverting (stable regime).
"""
from collections import deque
from typing import Dict
import logging

logger = logging.getLogger(__name__)


class AdaptiveBiasFilter:
    def __init__(self, k_base: float = 0.3, k_max: float = 0.7, window: int = 5):
        self.k_base = k_base
        self.k_max = k_max
        self.window = window
        self._bias: Dict[str, float] = {}
        self._errors: Dict[str, deque] = {}

    def update(self, city: str, actual_f: float, forecast_f: float) -> float:
        """Record new observation. Returns updated bias estimate."""
        e = actual_f - forecast_f
        if city not in self._errors:
            self._errors[city] = deque(maxlen=self.window)
        self._errors[city].append(e)
        K = self._gain(city)
        b_prev = self._bias.get(city, 0.0)
        b_new = b_prev + K * (e - b_prev)
        self._bias[city] = b_new
        logger.debug("BIAS_FILTER %s | e=%.2f | K=%.3f | bias: %.3f -> %.3f",
                     city, e, K, b_prev, b_new)
        return b_new

    def correction(self, city: str) -> float:
        """Current bias correction to add to forecast (positive = forecast runs cold)."""
        return self._bias.get(city, 0.0)

    def _gain(self, city: str) -> float:
        errors = list(self._errors.get(city, []))
        if len(errors) < 2:
            return self.k_base
        same_sign = sum(1 for a, b in zip(errors, errors[1:]) if a * b > 0)
        persistence = same_sign / (len(errors) - 1)
        return self.k_base + (self.k_max - self.k_base) * persistence
