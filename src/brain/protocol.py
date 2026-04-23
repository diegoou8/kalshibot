from typing import Protocol, Dict, Any, runtime_checkable


@runtime_checkable
class BrainModel(Protocol):
    """
    Pluggable ML brain interface.
    Implementors estimate P(YES settles) per market given market data + particle filter posterior.
    When brain=None, DecisionEngine falls back to conservative rule-based defaults.
    """

    def predict(self, market: Dict[str, Any], posterior: Dict[str, Any]) -> float:
        """
        Returns P(YES settles) in (0, 1).

        market:    raw Kalshi market dict — keys: ticker, yes_ask, yes_bid (or no_ask), depth, spread
        posterior: particle filter enrichment — keys: P_adj_YES, P_true_YES, posterior_var_T,
                   tau_hrs, pi_stale.  May be empty dict for rule-only callers.
        """
        ...
