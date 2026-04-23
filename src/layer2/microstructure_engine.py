from .models import MarketTick, MicrostructureFeatures

class MicrostructureEngine:
    def process(self, tick: MarketTick, last_forecast_ts: float) -> MicrostructureFeatures:
        mid_price = (tick.best_bid + tick.best_ask) / 2.0
        spread = tick.best_ask - tick.best_bid
        total_depth = tick.bid_depth + tick.ask_depth
        time_since_forecast = tick.timestamp - last_forecast_ts if last_forecast_ts > 0 else 99999.0
        
        return MicrostructureFeatures(
            timestamp=tick.timestamp,
            ticker=tick.ticker,
            target_id=tick.target_id,
            mid_price=mid_price,
            spread=spread,
            total_depth=total_depth,
            bid_depth=tick.bid_depth,
            ask_depth=tick.ask_depth,
            aggressor_imbalance=tick.aggressor_imbalance,
            volume_spike=tick.volume_spike,
            time_since_forecast=time_since_forecast
        )
