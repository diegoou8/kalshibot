import numpy as np
from typing import List, Dict, Tuple, Optional
import math
import logging

logger = logging.getLogger(__name__)

class ForecastStore:
    def __init__(self, lambda_age: float = 0.005, epsilon_sigma: float = 1e-4):
        self.vintages: Dict[str, Dict] = {}
        self.lambda_age = lambda_age
        self.epsilon_sigma = epsilon_sigma

    def update_forecast(self, source: str, value: float, bias: float, sigma_sq: float, issued_at: float):
        """Update or insert a forecast vintage from a specific source."""
        self.vintages[source] = {
            'value': value,
            'bias': bias,
            'sigma_sq': sigma_sq,
            'issued_at': issued_at
        }
        
    def get_anchor(self, current_time: float) -> Tuple[float, float, List[Dict]]:
        """
        Compute the precision-weighted multi-vintage forecast anchor μ_anc,t and V_anc,t.
        Returns: (mu_anc_t, V_anc_t, active_vintages_list)
        """
        if not self.vintages:
            return 0.0, 0.0, []
            
        unnorm_weights = []
        bias_corrected_values = []
        raw_vintages = []
        
        for src, v in self.vintages.items():
            age_min = max(0.0, (current_time - v['issued_at']) / 60.0)
            
            # Unnormalized weight formula
            omega_tilde = math.exp(-self.lambda_age * age_min) * (1.0 / (v['sigma_sq'] + self.epsilon_sigma))
            f_tilde = v['value'] - v['bias']
            
            unnorm_weights.append(omega_tilde)
            bias_corrected_values.append(f_tilde)
            
            raw_vintages.append({
                'source': src,
                'bias_corrected': f_tilde,
                'sigma_sq': v['sigma_sq'],
                'age_min': age_min
            })
            
        total_w = sum(unnorm_weights)
        if total_w == 0:
            return 0.0, 0.0, []
            
        weights = [w / total_w for w in unnorm_weights]
        
        # Compute anchor (mean) and disagreement (variance)
        mu_anc = sum(w * f for w, f in zip(weights, bias_corrected_values))
        v_anc = sum(w * ((f - mu_anc) ** 2) for w, f in zip(weights, bias_corrected_values))
        
        for k in range(len(raw_vintages)):
            raw_vintages[k]['normalized_weight'] = weights[k]
            
        return mu_anc, v_anc, raw_vintages


class TemperatureParticleFilter:
    """
    SMC Particle Filter maintaining posterior over T* (Settlement Temperature).
    Implements continuous OU propagation, Student-t Forecast Likelihood,
    and Mixture Logit Market Likelihood with Softened Interval Mapping.
    """
    def __init__(self, 
                 N: int = 2000, 
                 sigma_init: float = 2.0, 
                 theta: float = 0.15,
                 sigma_floor: float = 0.32,
                 beta_0: float = 0.3, 
                 beta_1: float = 0.5, 
                 beta_2: float = 0.04,
                 gamma: float = 2.0,
                 rho_ESS: float = 0.35,
                 lambda_f: float = 1.0,
                 lambda_mkt: float = 1.0,
                 nu_f: float = 5.0,
                 h_band: float = 10.0):
        
        self.N = N
        self.sigma_init = sigma_init
        self.theta = theta
        self.sigma_floor = sigma_floor
        self.beta_0 = beta_0
        self.beta_1 = beta_1
        self.beta_2 = beta_2
        self.gamma = gamma
        self.rho_ESS = rho_ESS
        self.lambda_f = lambda_f
        self.lambda_mkt = lambda_mkt
        self.nu_f = nu_f
        self.h_band = h_band
        
        # State
        self.particles = np.zeros(self.N)
        self.weights = np.ones(self.N) / self.N
        self.mu_anc = 0.0
        self.V_anc = 0.0
        self.t_last_prop: Optional[float] = None
        self.is_initialized = False

    def initialize(self, mu_anc_0: float, V_anc_0: float, current_time: float):
        """Initialize particle cloud centered on the multi-vintage anchor."""
        self.mu_anc = mu_anc_0
        self.V_anc = V_anc_0
        self.particles = np.random.normal(loc=self.mu_anc, scale=self.sigma_init, size=self.N)
        self.weights = np.ones(self.N) / self.N
        self.t_last_prop = current_time
        self.is_initialized = True

    def ess(self) -> float:
        """Effective Sample Size."""
        return 1.0 / np.sum(self.weights ** 2)

    def resample(self):
        """Systematic Resampling."""
        cumulative_sum = np.cumsum(self.weights)
        cumulative_sum[-1] = 1.0
        
        step = 1.0 / self.N
        u = np.random.uniform(0.0, step)
        
        indices = np.zeros(self.N, dtype=int)
        j = 0
        for i in range(self.N):
            while u > cumulative_sum[j]:
                j += 1
            indices[i] = j
            u += step
            
        self.particles = self.particles[indices]
        self.weights = np.ones(self.N) / self.N

    def conditional_resample(self) -> bool:
        if self.ess() < self.rho_ESS * self.N:
            self.resample()
            return True
        return False

    def propagate(self, current_time: float, resolution_time: float, mu_anc: float, V_anc: float):
        """Ornstein-Uhlenbeck (OU) Transition Model. Time in seconds converted to hours."""
        if not self.is_initialized or self.t_last_prop is None:
            self.initialize(mu_anc, V_anc, current_time)
            return

        dt_sec = max(0.0, current_time - self.t_last_prop)
        if dt_sec <= 0:
            return
            
        dt_hrs = dt_sec / 3600.0
        tau_settle = max(0.0, (resolution_time - current_time) / 3600.0)
        
        self.mu_anc = mu_anc
        self.V_anc = V_anc
        
        # Transition variance equation
        sigma_trans_sq = max(self.sigma_floor**2, 
                             self.beta_0 + self.beta_1 * self.V_anc + self.beta_2 * tau_settle)
        sigma_trans = np.sqrt(sigma_trans_sq)
        
        # OU update
        drift = self.theta * (self.mu_anc - self.particles) * dt_hrs
        noise = sigma_trans * np.sqrt(dt_hrs) * np.random.normal(0, 1, self.N)
        
        self.particles += drift + noise
        self.t_last_prop = current_time

    def apply_forecast_jump_blend(self, prev_mu_anc: float, jump_threshold: float = 1.0, alpha: float = 0.2):
        """
        When the forecast anchor jumps significantly, blend a fraction of particles
        toward the new anchor rather than waiting for OU drift to catch up.
        Implements the jump component of the Logit Jump-Diffusion model.
        """
        delta = abs(self.mu_anc - prev_mu_anc)
        if delta < jump_threshold or not self.is_initialized:
            return
        n_refresh = max(1, int(alpha * self.N))
        refresh_indices = np.random.choice(self.N, n_refresh, replace=False)
        self.particles[refresh_indices] = np.random.normal(
            loc=self.mu_anc, scale=self.sigma_init * 0.5, size=n_refresh
        )
        self._normalize_and_manage_ESS()

    def apply_forecast_likelihood(self, active_vintages: List[Dict]):
        """Stream A: Apply Student-t likelihood from weather forecast updates."""
        if not active_vintages:
            return
            
        for v in active_vintages:
            f_tilde = v['bias_corrected']
            sigma_sq = v['sigma_sq']
            
            # Student-t likelihood proportionality (ignoring constants)
            tmp = 1.0 + ((f_tilde - self.particles) ** 2) / (self.nu_f * sigma_sq)
            L_forecast = tmp ** (-(self.nu_f + 1) / 2.0)
            
            # Safe multiplication
            self.weights *= (L_forecast ** self.lambda_f)
            
        self._normalize_and_manage_ESS()

    def get_soft_band_probabilities(self, bands: List[Tuple[float, float]]) -> np.ndarray:
        """
        Softened Interval Mapping implementing normalized sigmoid boundaries.
        Returns: p_{k,t}^{(i)} of shape (N, K) - Simplex-Compatible.
        """
        K = len(bands)
        P_raw = np.zeros((self.N, K))
        
        for k, (L_k, U_k) in enumerate(bands):
            sig_L = 1.0 / (1.0 + np.exp(-self.gamma * (self.particles - L_k)))
            sig_U = 1.0 / (1.0 + np.exp(-self.gamma * (self.particles - U_k)))
            P_raw[:, k] = np.maximum(0.0, sig_L - sig_U)
            
        # Numerical protection for row_sum division
        row_sums = np.sum(P_raw, axis=1, keepdims=True)
        row_sums = np.maximum(1e-12, row_sums)
        
        P_norm = P_raw / row_sums
        return P_norm

    def get_active_band_indices(self, bands: List[Tuple[float, float]]) -> List[int]:
        """Formal helper for Section 6.2 active band selection."""
        mu_t = np.sum(self.weights * self.particles)
        active_indices = []
        for k, (L_k, U_k) in enumerate(bands):
            center_k = (L_k + U_k) / 2.0
            if abs(center_k - mu_t) <= self.h_band:
                active_indices.append(k)
        return active_indices

    def apply_market_likelihood(self, active_bands: List[Dict], all_bands: List[Tuple[float, float]]):
        """
        Stream B: Apply Logistic Mixture Likelihood from eligible orderbook ticks.
        active_bands: List of {'index': int, 'q_mid': float[0-1], 'v_mkt': float, 'pi_stale': float}
        """
        if not active_bands:
            return
            
        P_norm = self.get_soft_band_probabilities(all_bands)
        epsilon_q = 0.001
        
        def logit(x):
            return np.log(x / (1.0 - x))
            
        for band in active_bands:
            k = band['index']
            # Clipping before logit
            q_bar = np.clip(band['q_mid'], epsilon_q, 1.0 - epsilon_q)
            p_bar = np.clip(P_norm[:, k], epsilon_q, 1.0 - epsilon_q)
            
            logit_q = logit(q_bar)
            logit_p = logit(p_bar)
            v_mkt = band['v_mkt']
            pi_stale = band['pi_stale']
            
            # Normal PDF proportionality (Gaussian constant intentionally omitted)
            L_mkt = np.exp(-((logit_q - logit_p) ** 2) / (2.0 * v_mkt))
            
            # Mixture Likelihood
            L_null = 1.0
            L_obs = (1.0 - pi_stale) * L_mkt + pi_stale * L_null
            
            self.weights *= (L_obs ** self.lambda_mkt)
            
        self._normalize_and_manage_ESS()

    def _normalize_and_manage_ESS(self):
        sum_w = np.sum(self.weights)
        if sum_w <= 1e-30 or np.isnan(sum_w):
            self.weights = np.ones(self.N) / self.N
            logger.warning("Filter weights catastrophic collapse; resetting to uniform.")
        else:
            self.weights /= sum_w
            
        self.conditional_resample()

    def daily_max_var(self, sigma_intraday: float = 2.0, mode: str = "half") -> float:
        """
        Return posterior variance adjusted for T_max (daily high).

        mode controls how much of the Gumbel correction is applied:
          "none" — raw OU variance, no Gumbel offset (point temperature only)
          "half" — sigma_intraday scaled by 0.5 (conservative; recommended until validated)
          "full" — full Gumbel offset (original behaviour)
        """
        mu_raw = float(np.sum(self.weights * self.particles))
        var_raw = float(np.sum(self.weights * (self.particles - mu_raw) ** 2))

        if mode == "none":
            return max(1.0, var_raw)

        scale_factor = 0.5 if mode == "half" else 1.0
        sigma_eff = sigma_intraday * scale_factor
        euler_mascheroni = 0.5772156649
        gumbel_scale = sigma_eff * math.pi / math.sqrt(6)
        gumbel_loc = sigma_eff * euler_mascheroni
        gumbel_offsets = np.random.gumbel(loc=gumbel_loc, scale=gumbel_scale, size=self.N)
        tmax_particles = self.particles + gumbel_offsets
        mu_max = float(np.sum(self.weights * tmax_particles))
        var_max = float(np.sum(self.weights * (tmax_particles - mu_max) ** 2))
        return max(1.0, var_max)

    def daily_max_p_above(self, threshold: float, sigma_intraday: float = 2.0, mode: str = "half") -> float:
        """P(daily_max > threshold) using Gumbel-transformed particle cloud."""
        if mode == "none":
            return float(np.sum(self.weights * (self.particles > threshold).astype(float)))
        scale_factor = 0.5 if mode == "half" else 1.0
        sigma_eff = sigma_intraday * scale_factor
        euler_mascheroni = 0.5772156649
        gumbel_scale = sigma_eff * math.pi / math.sqrt(6)
        gumbel_loc = sigma_eff * euler_mascheroni
        gumbel_offsets = np.random.gumbel(loc=gumbel_loc, scale=gumbel_scale, size=self.N)
        tmax_particles = self.particles + gumbel_offsets
        return float(np.sum(self.weights * (tmax_particles > threshold).astype(float)))

    def get_posterior_stats(self, bands: List[Tuple[float, float]]) -> Dict:
        """Computes outputs for EV Engine."""
        mu_t = np.sum(self.weights * self.particles)
        var_t_star = np.sum(self.weights * ((self.particles - mu_t) ** 2))
        
        P_norm = self.get_soft_band_probabilities(bands)
        P_true = np.sum(self.weights[:, None] * P_norm, axis=0)
        Var_P_k = np.sum(self.weights[:, None] * ((P_norm - P_true) ** 2), axis=0)
        
        fragility_t = min(min(abs(mu_t - L_k), abs(mu_t - U_k)) for (L_k, U_k) in bands)
        
        return {
            "mu_t": mu_t,
            "var_t_star": var_t_star,
            "P_true": P_true.tolist(), # List of shape (K,)
            "Var_P_k": Var_P_k.tolist(),
            "fragility_t": fragility_t,
            "ESS": self.ess()
        }
