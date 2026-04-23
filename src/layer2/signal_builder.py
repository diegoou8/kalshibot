import numpy as np
from .models import SignalPackage, PosteriorState, ForecastState, RiskMetrics, EdgeMetrics, MarketTick

class SignalBuilder:
    def build(self, tick: MarketTick, posterior: PosteriorState, forecast: ForecastState, risk: RiskMetrics, edge: EdgeMetrics, regime: str, model_prob: float, all_probs: dict) -> SignalPackage:
        return SignalPackage(
            ticker=tick.ticker,
            target_id=tick.target_id,
            market_price=(tick.best_bid + tick.best_ask) / 2.0,
            model_probability=model_prob,
            EV_raw=edge.EV_raw,
            EV_adjusted=edge.EV_adjusted,
            posterior_temp_mean=posterior.mean_temp,
            posterior_temp_std=np.sqrt(posterior.var_temp),
            forecast_revision=forecast.revision_delta,
            confidence=risk.confidence_score,
            regime_label=regime,
            all_probabilities=all_probs
        )
