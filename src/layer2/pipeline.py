import time
import math
import logging
from typing import List, Dict, Tuple, Optional
from src.layer2.particle_filter import TemperatureParticleFilter, ForecastStore
from src.layer2.ev_engine import EVEngine, KalshiFeeModel, ExecutionEstimator
from src.layer2.gating_logic import TradeGating

logger = logging.getLogger(__name__)

class Layer2Pipeline:
    """
    Event coordinator for Layer 2.
    Routes Stream A (Forecast) and Stream B (Market) events.
    Enforces inference-only for forecasts and decision-emission for market ticks.
    """
    def __init__(self, 
                 filter_params: Dict,
                 gating_params: Dict,
                 estimator_params: Dict,
                 bands: List[Tuple[float, float]],
                 resolution_time: float):
        
        self.bands = bands
        self.resolution_time = resolution_time
        
        # Core components
        self.forecast_store = ForecastStore()
        self.filter = TemperatureParticleFilter(**filter_params)
        self.ev_engine = EVEngine(KalshiFeeModel(), ExecutionEstimator(**estimator_params))
        self.gating = TradeGating(**gating_params)
        
        # State tracking
        self.last_mu_anc = 0.0
        self.c_var = 0.5
        self.c_stale = 0.3

    def handle_forecast_event(self, timestamp: float, source: str, value: float, bias: float, sigma_sq: float):
        """
        Stream A: Update inference state. No trade decision emitted.
        """
        self.forecast_store.update_forecast(source, value, bias, sigma_sq, timestamp)
        mu_anc, v_anc, active_vintages = self.forecast_store.get_anchor(timestamp)
        
        # Propagate and apply forecast likelihood
        self.filter.propagate(timestamp, self.resolution_time, mu_anc, v_anc)
        self.filter.apply_forecast_likelihood(active_vintages)
        
        # Handle jump blending if anchor shifted significantly
        self.filter.apply_forecast_jump_blend(self.last_mu_anc)
        self.last_mu_anc = mu_anc
        
        logger.info(f"Layer 2: Forecast Ingested ({source} at {value}). ESS: {self.filter.ess():.1f}")

    def handle_market_event(self, 
                            timestamp: float, 
                            band_market_data: List[Dict]) -> List[Dict]:
        """
        Stream B: Update inference and emit trade decisions.
        band_market_data: List of {
            'index': int, 
            'q_mid': float,
            'bid': float, 
            'ask': float, 
            'spread': float,
            'depth_bid': int,
            'depth_ask': int,
            'ask_ladder': List[(price, qty)],
            'bid_ladder': List[(price, qty)],
            'pi_stale': float,
            'v_mkt': float,
            'velocity': float
        }
        """
        # 1. Propagate particles to current tick time
        mu_anc, v_anc, _ = self.forecast_store.get_anchor(timestamp)
        self.filter.propagate(timestamp, self.resolution_time, mu_anc, v_anc)
        
        # 2. Filter for active bands (Section 6.2)
        active_indices = self.filter.get_active_band_indices(self.bands)
        active_eligible_data = [d for d in band_market_data if d['index'] in active_indices]
        
        # 3. Apply market likelihood
        self.filter.apply_market_likelihood(active_eligible_data, self.bands)
        
        # 4. Compute Posterior Stats
        stats = self.filter.get_posterior_stats(self.bands)
        
        # 5. Build Decision Payload per band
        decisions = []
        tau_settle_hrs = (self.resolution_time - timestamp) / 3600.0
        
        for d in band_market_data:
            k = d['index']
            p_true = stats['P_true'][k]
            var_pk = stats['Var_P_k'][k]
            
            # Confidence-Adjusted Probability (Section 10.1)
            p_adj = max(0.0, min(1.0, p_true - self.c_var * math.sqrt(var_pk) - self.c_stale * d['pi_stale']))
            
            # EV Calculation per side
            # Buy YES: Uses YES Ask ladder
            # Buy NO:  Uses NO Ask ladder (Market provides NO ask as 100 - Bid_YES)
            ev_yes = self.ev_engine.calculate_ev("YES", 10, p_adj, d['ask_ladder'], d['spread'], d['velocity'])
            ev_no = self.ev_engine.calculate_ev("NO", 10, p_adj, d['no_ask_ladder'], d['spread'], d['velocity'])
            
            # Determine best side
            best_ev = max(ev_yes['ev_cents'], ev_no['ev_cents'])
            selected_side = "YES" if ev_yes['ev_cents'] > ev_no['ev_cents'] else "NO"
            result_ev = ev_yes if selected_side == "YES" else ev_no
            
            # 6. Gating Logic
            is_exec, gating_reasons = self.gating.evaluate(
                side=selected_side,
                ev_cents=best_ev,
                pi_stale=d['pi_stale'],
                spread=d['spread'],
                fragility=stats['fragility_t'],
                ess=stats['ESS'],
                n_particles=self.filter.N,
                depth=d['depth_ask'] if selected_side == "YES" else d['depth_bid'],
                tau_settle_hrs=tau_settle_hrs,
                posterior_var=stats['var_t_star']
            )
            
            # 7. Construct schema (Section 13)
            payload = {
                "contract_id": d.get('contract_id'),
                "band_id": f"{self.bands[k][0]}_{self.bands[k][1]}",
                "timestamp": timestamp,
                "posterior_mean_T": stats['mu_t'],
                "posterior_var_T": stats['var_t_star'],
                "P_true_YES": p_true,
                "P_adj_YES": p_adj,
                "EV_yes_cents": ev_yes['ev_cents'],
                "EV_no_cents": ev_no['ev_cents'],
                "side": selected_side if best_ev > 0 else "NONE",
                "execute_flag": is_exec,
                "reason_codes": gating_reasons,
                "market_obs_variance": d['v_mkt'],
                "stale_prob": d['pi_stale'],
                "expected_slippage_cents": result_ev.get('slip_total_cents', 0.0),
                "fee_cents": result_ev.get('fee_cents', 0.0)
            }
            decisions.append(payload)
            
        return decisions
