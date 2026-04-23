from dataclasses import dataclass, field
from typing import List, Dict, Optional
import numpy as np

@dataclass
class ForecastUpdate:
    timestamp: float
    target_id: str  # e.g., "NYC_2026-03-09"
    projected_temp: float
    uncertainty: float = 1.0

@dataclass
class MarketTick:
    timestamp: float
    ticker: str
    target_id: str
    lower_bound: float
    upper_bound: float
    best_bid: float
    best_ask: float
    bid_depth: float
    ask_depth: float
    aggressor_imbalance: float
    volume_spike: bool = False

@dataclass
class MicrostructureFeatures:
    timestamp: float
    ticker: str
    target_id: str
    mid_price: float
    spread: float
    total_depth: float
    bid_depth: float
    ask_depth: float
    aggressor_imbalance: float
    volume_spike: bool
    time_since_forecast: float

@dataclass
class ForecastState:
    target_id: str
    current_mu: float
    revision_delta: float
    last_update_ts: float
    uncertainty: float

@dataclass
class PosteriorState:
    target_id: str
    particles: np.ndarray
    weights: np.ndarray
    mean_temp: float
    var_temp: float

@dataclass
class IntervalProbabilities:
    target_id: str
    probabilities: Dict[str, float]  # ticker -> prob

@dataclass
class RiskMetrics:
    target_id: str
    posterior_variance: float
    entropy: float
    confidence_score: float
    uncertainty_regime: bool
    spread_risk: float
    depth_risk: float

@dataclass
class EdgeMetrics:
    ticker: str
    EV_raw: float
    EV_adjusted: float

@dataclass
class SignalPackage:
    ticker: str
    target_id: str
    market_price: float
    model_probability: float
    EV_raw: float
    EV_adjusted: float
    posterior_temp_mean: float
    posterior_temp_std: float
    forecast_revision: float
    confidence: float
    regime_label: str
    all_probabilities: Dict[str, float] = field(default_factory=dict)
