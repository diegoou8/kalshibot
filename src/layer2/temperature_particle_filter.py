import numpy as np
from typing import Dict, Any
from .models import ForecastState, PosteriorState, MicrostructureFeatures

class TemperatureParticleFilter:
    def __init__(self, num_particles: int = 2000):
        self.num_particles = num_particles
        self.particles: Dict[str, np.ndarray] = {}
        self.weights: Dict[str, np.ndarray] = {}
        self.last_ts: Dict[str, float] = {}
    
    def init_particles(self, target_id: str, mu_0: float, ts: float):
        if target_id not in self.particles:
            self.particles[target_id] = np.random.normal(mu_0, 2.0, self.num_particles)
            self.weights[target_id] = np.ones(self.num_particles) / self.num_particles
            self.last_ts[target_id] = ts

    def update(self, target_id: str, ts: float, forecast: ForecastState, obs_prob: float, obs_interval: tuple, sigma_obs_sq: float) -> PosteriorState:
        # Time step
        dt = max(ts - self.last_ts[target_id], 0.001)
        self.last_ts[target_id] = ts
        
        particles = self.particles[target_id]
        weights = self.weights[target_id]
        
        # Exact OU Process transition
        kappa = 0.01 # mean-reversion 
        sigma_proc = 0.5 + min(1.0, abs(forecast.revision_delta))
        
        # Determine actual noise variance
        var = (sigma_proc**2 / (2*kappa)) * (1 - np.exp(-2*kappa*dt)) if kappa > 0 else (sigma_proc**2)*dt
        noise = np.random.normal(0, np.sqrt(var), self.num_particles)
        
        # Exponential mean reversion
        particles = forecast.current_mu + (particles - forecast.current_mu) * np.exp(-kappa * dt) + noise
        
        # Observation weighing. 
        # For this contract interval [L, U), the market says P(T in [L,U)) = obs_prob.
        # How likely is this observation?
        # A simple approximation: if a particle is inside [L,U), its implied P_i = 1, else 0.
        # We can smooth it using a sigmoid.
        L, U = obs_interval
        # smooth indicator
        temp_widths = U - L
        p_implied = sc_expit(10.0 * (particles - L)) - sc_expit(10.0 * (particles - U))
        
        # Likelihood of observing obs_prob given the particle's deterministic p_implied
        likelihood = np.exp(-0.5 * ((p_implied - obs_prob)**2) / sigma_obs_sq)
        
        weights *= likelihood
        weights += 1e-300
        weights /= np.sum(weights)
        
        # Resampling
        ess = 1.0 / np.sum(weights**2)
        if ess < self.num_particles / 3.0:
            indices = np.random.choice(self.num_particles, self.num_particles, p=weights)
            particles = particles[indices]
            weights = np.ones(self.num_particles) / self.num_particles
            
        self.particles[target_id] = particles
        self.weights[target_id] = weights
        
        mean_temp = float(np.average(particles, weights=weights))
        var_temp = float(np.average((particles - mean_temp)**2, weights=weights))
        
        return PosteriorState(
            target_id=target_id,
            particles=particles.copy(),
            weights=weights.copy(),
            mean_temp=mean_temp,
            var_temp=var_temp
        )

def sc_expit(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -50, 50)))
