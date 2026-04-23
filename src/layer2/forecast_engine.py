from typing import Dict, Optional
from .models import ForecastUpdate, ForecastState

class ForecastEngine:
    def __init__(self):
        self.state: Dict[str, ForecastState] = {}

    def process_update(self, update: ForecastUpdate) -> ForecastState:
        target_id = update.target_id
        if target_id not in self.state:
            new_state = ForecastState(
                target_id=target_id,
                current_mu=update.projected_temp,
                revision_delta=0.0,
                last_update_ts=update.timestamp,
                uncertainty=update.uncertainty
            )
        else:
            prev = self.state[target_id]
            revision_delta = update.projected_temp - prev.current_mu
            new_state = ForecastState(
                target_id=target_id,
                current_mu=update.projected_temp,
                revision_delta=revision_delta,
                last_update_ts=update.timestamp,
                uncertainty=update.uncertainty
            )
        
        self.state[target_id] = new_state
        return new_state

    def get_state(self, target_id: str) -> Optional[ForecastState]:
        return self.state.get(target_id)
