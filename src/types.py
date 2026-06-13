from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional, TypedDict

Coin = Literal["btc", "eth", "sol", "xrp"]
Minutes = Literal[15, 60, 240, 1440]

class MarketConfig(TypedDict):
    coin: Coin
    minutes: Minutes


@dataclass(frozen=True)
class ContractSemantics:
    """
    Verified semantics for a Kalshi weather market contract.
    Built from ticker + live Kalshi market metadata dict.

    verified=True only when direction is unambiguous:
      - BAND/HOURLY: always verified from ticker structure
      - THRESHOLD (T-type daily): verified only when strike_type is present
        in the live market dict returned by get_weather_markets()

    When verified=False, the trade is skipped before EV calculation and
    BLOCKED_UNVERIFIED_CONTRACT_SEMANTICS is logged.
    """
    ticker:          str
    canonical_city:  Optional[str]
    market_type:     Optional[str]   # HIGH_BAND | HIGH_ABOVE | HOURLY_ABOVE
    contract_type:   Optional[str]   # BAND | THRESHOLD | HOURLY
    direction:       Optional[str]   # ABOVE | BELOW | BAND
    threshold:       Optional[float]
    floor_strike:    Optional[float]
    cap_strike:      Optional[float]
    settlement_date: Optional[str]   # YYYY-MM-DD
    settlement_hour: Optional[int]
    verified:        bool
    failure_reason:  Optional[str]
