"""
Logit Jump-Diffusion Brain (RN-JD).

Theory (from quant desk simulation notebook):
  - Settlement probability p_t lives in logit space: x_t = log(p_t / (1 - p_t))
  - x_t follows an SDE with a risk-neutral drift that keeps E[p_t] a Q-martingale
  - Our particle filter posterior is the primary signal (edge source)
  - Market mid-price is a secondary signal that adjusts our estimate modestly
  - Jump component handled upstream by TemperatureParticleFilter.apply_forecast_jump_blend()

Design principle: our model has the majority weight (at least 60%).  The market
adjusts us; we don't blindly follow the market — that's where the edge lives.

Calibration target: Brier score < 0.25 (see tests/test_brain.py).
"""
import math
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class LogitJumpDiffusionBrain:
    """
    Fuses SMC particle filter posterior with live orderbook signal in logit space.

    Parameters
    ----------
    sigma_belief  : belief volatility σ_b — controls risk-neutral drift magnitude
    kappa_mkt     : base weight given to the market mid-price signal (0–1).
                    Wide spread or low depth shrinks this further.
                    Our model always keeps at least (1 - max_alpha_mkt) weight.
    max_alpha_mkt : hard ceiling on market weight (default 0.40 → model ≥ 60%)
    min_prob      : hard floor on output probability
    max_prob      : hard ceiling on output probability
    """

    def __init__(
        self,
        sigma_belief: float = 0.3,
        kappa_mkt: float = 0.3,
        max_alpha_mkt: float = 0.40,
        min_prob: float = 0.02,
        max_prob: float = 0.98,
    ):
        self.sigma_belief  = sigma_belief
        self.kappa_mkt     = kappa_mkt
        self.max_alpha_mkt = max_alpha_mkt
        self.min_prob      = min_prob
        self.max_prob      = max_prob

    @staticmethod
    def _logit(p: float) -> float:
        p = max(1e-6, min(1.0 - 1e-6, p))
        return math.log(p / (1.0 - p))

    @staticmethod
    def _sigmoid(x: float) -> float:
        if x >= 0:
            return 1.0 / (1.0 + math.exp(-x))
        e = math.exp(x)
        return e / (1.0 + e)

    def predict(self, market: Dict[str, Any], posterior: Dict[str, Any]) -> float:
        """
        Returns calibrated P(YES settles) in (min_prob, max_prob).

        Our particle filter posterior is the primary opinion.
        Market mid adjusts it with a weight that shrinks as spread widens.
        """
        # ── Extract posterior from particle filter ───────────────────────────
        p_adj: Optional[float] = posterior.get("P_adj_YES")
        tau_hrs: float         = float(posterior.get("tau_hrs", 12.0))
        pi_stale: float        = float(posterior.get("pi_stale", 0.5))

        # ── Extract market microstructure ────────────────────────────────────
        yes_ask: float = float(market.get("yes_ask", 50))
        yes_bid: float = float(market.get("yes_bid", market.get("yes_ask", 50) - 5))
        depth: float   = float(market.get("depth", 10))

        spread_cents = max(yes_ask - yes_bid, 1.0)

        # ── Step 1: Prior in logit space (particle filter is our edge) ───────
        if p_adj is not None and 0.0 < p_adj < 1.0:
            x_prior = self._logit(p_adj)
        else:
            # No particle filter data — fall back to market mid with high uncertainty
            mid = max(0.02, min(0.98, (yes_ask + yes_bid) / 200.0))
            x_prior = self._logit(mid)

        # ── Step 2: Market signal in logit space ─────────────────────────────
        q_mid = max(0.02, min(0.98, (yes_ask + yes_bid) / 200.0))
        x_mkt = self._logit(q_mid)

        # ── Step 3: Adaptive market weight (volume-gated softmax) ────────────
        # Spread/depth reduce trust; volume raises it (informed traders active
        # in high-volume periods — notebook: softmax gate on market sensor).
        volume: float = float(market.get("volume", 0) or 0)
        spread_penalty = min(spread_cents / 20.0, 1.0)   # 0 (tight) → 1 (wide)
        depth_penalty  = min(10.0 / max(depth, 1.0), 1.0)  # 0 (deep) → 1 (shallow)
        # log(1 + v/100) / log(1 + 1000/100) normalises volume to ~[0, 1]
        vol_factor = math.log1p(volume / 100.0) / math.log1p(10.0)
        vol_factor = max(0.0, min(1.0, vol_factor))
        alpha_mkt = self.kappa_mkt * (
            1.0 - 0.4 * spread_penalty - 0.1 * depth_penalty + 0.2 * vol_factor
        )
        alpha_mkt = max(0.05, min(self.max_alpha_mkt, alpha_mkt))

        # ── Step 4: Blend in logit space (our model is dominant) ─────────────
        x_post = (1.0 - alpha_mkt) * x_prior + alpha_mkt * x_mkt

        # ── Step 5: Risk-neutral drift correction (formal Itô) ───────────────
        # For p_t = σ(x_t) to be a Q-martingale, Itô's lemma requires:
        #   μ = 0.5 · S''(x)/S'(x) · σ_b²  where S''(x)/S'(x) = tanh(x/2)
        # Accumulated over τ hours: Δx = μ · (τ/24)
        # Scale sigma_belief by the PF temperature uncertainty.
        # Reference: sigma_init=2.0°F → var_ref=4.0°F².
        # High forecast uncertainty (long horizon) → larger drift correction.
        # Low uncertainty (short horizon) → smaller correction.
        var_T    = float(posterior.get("posterior_var_T", 4.0))
        sigma_eff = self.sigma_belief * math.sqrt(max(0.25, var_T) / 4.0)

        rn_drift = (
            0.5 * sigma_eff ** 2
            * math.tanh(x_post / 2.0)
            * min(tau_hrs, 24.0) / 24.0
        )
        x_post += rn_drift

        # ── Step 6: Staleness penalty ─────────────────────────────────────────
        # Stale market → discount the market component → pull log-odds toward 0
        x_post *= (1.0 - 0.3 * min(pi_stale, 1.0))

        # ── Step 7: Convert back to probability ───────────────────────────────
        p_out = self._sigmoid(x_post)
        result = float(max(self.min_prob, min(self.max_prob, p_out)))

        logger.debug(
            "Brain predict: ticker=%s p_adj=%.3f q_mid=%.3f tau=%.1fh "
            "alpha_mkt=%.2f x_prior=%.3f x_mkt=%.3f x_post=%.3f → p=%.3f",
            market.get("ticker", "?"),
            p_adj if p_adj is not None else float("nan"),
            q_mid, tau_hrs, alpha_mkt, x_prior, x_mkt, x_post, result,
        )
        return result
