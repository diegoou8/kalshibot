from typing import List, Dict, Tuple, Optional
import numpy as np

class TradeGating:
    """
    Implements the 8 Trade Gates from Section 11 of the Layer 2 blueprint.
    Emits execute_flag = true only when all conditions are satisfied.
    """
    def __init__(self, 
                 ev_min: float = 3.0, 
                 pi_stale_max: float = 0.30, 
                 s_max: float = 8.0, 
                 f_min: float = 1.5, 
                 ess_min_fraction: float = 0.20, 
                 d_min: int = 10, 
                 tau_min_hrs: float = 0.5, 
                 v_max: Optional[float] = None):
        
        self.ev_min = ev_min
        self.pi_stale_max = pi_stale_max
        self.s_max = s_max
        self.f_min = f_min
        self.ess_min_fraction = ess_min_fraction
        self.d_min = d_min
        self.tau_min_hrs = tau_min_hrs
        self.v_max = v_max

    def evaluate(self, 
                 side: str, 
                 ev_cents: float, 
                 pi_stale: float, 
                 spread: float, 
                 fragility: float, 
                 ess: float, 
                 n_particles: int, 
                 depth: int, 
                 tau_settle_hrs: float, 
                 posterior_var: float) -> Tuple[bool, List[str]]:
        
        reasons = []
        is_executing = True
        
        # 1. EV gate
        if ev_cents <= self.ev_min:
            reasons.append(f"EV_FAIL: {ev_cents:.2f} <= {self.ev_min}")
            is_executing = False
            
        # 2. Stale gate
        if pi_stale >= self.pi_stale_max:
            reasons.append(f"STALE_FAIL: {pi_stale:.2f} >= {self.pi_stale_max}")
            is_executing = False
            
        # 3. Spread gate
        if spread > self.s_max:
            reasons.append(f"SPREAD_FAIL: {spread:.1f} > {self.s_max}")
            is_executing = False
            
        # 4. Boundary safety gate (Fragility)
        if fragility <= self.f_min:
            reasons.append(f"FRAGILITY_FAIL: {fragility:.2f} <= {self.f_min}")
            is_executing = False
            
        # 5. ESS gate
        ess_min = self.ess_min_fraction * n_particles
        if ess < ess_min:
            reasons.append(f"ESS_FAIL: {ess:.1f} < {ess_min}")
            is_executing = False
            
        # 6. Depth gate
        if depth < self.d_min:
            reasons.append(f"DEPTH_FAIL: {depth} < {self.d_min}")
            is_executing = False
            
        # 7. Settlement window gate
        if tau_settle_hrs < self.tau_min_hrs:
            reasons.append(f"SETTLEMENT_FAIL: {tau_settle_hrs:.2f} < {self.tau_min_hrs}")
            is_executing = False
            
        # 8. Optional variance gate
        if self.v_max is not None and posterior_var > self.v_max:
            reasons.append(f"VARIANCE_FAIL: {posterior_var:.2f} > {self.v_max}")
            is_executing = False
            
        if is_executing:
            reasons.append(f"EV_{side}_PASS")
            
        return is_executing, reasons
