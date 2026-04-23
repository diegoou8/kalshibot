from enum import Enum
from typing import Literal, TypedDict

Coin = Literal["btc", "eth", "sol", "xrp"]
Minutes = Literal[15, 60, 240, 1440]

class MarketConfig(TypedDict):
    coin: Coin
    minutes: Minutes
