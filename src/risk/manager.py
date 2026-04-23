import logging
from pathlib import Path
from typing import Optional

from ..config.env import Config
from ..db.dwtrader import DWTraderDB
from ..decision.engine import TradeIntent

logger = logging.getLogger(__name__)

_HALT_FLAG = Path(__file__).resolve().parents[2] / "data" / "halt.flag"


class RiskManager:
    """
    Guardrails and idempotency checks prior to execution.
    Checks: halt flag, position size limit, daily spend limit.
    """

    def __init__(self, db: DWTraderDB):
        self.db = db
        self.daily_loss_limit = Config.DAILY_LOSS_LIMIT
        self.max_position_size = Config.MAX_POSITION_SIZE

    def _is_halted(self) -> bool:
        if _HALT_FLAG.exists():
            logger.warning("TRADING HALTED — halt.flag present.")
            return True
        return False

    def _set_halt(self, reason: str) -> None:
        _HALT_FLAG.parent.mkdir(parents=True, exist_ok=True)
        _HALT_FLAG.write_text(reason)
        logger.critical("HALT FLAG SET: %s", reason)

    def preflight_check(self, intent: TradeIntent, env_mode: str) -> bool:
        """
        Returns True if APPROVED, False if BLOCKED.
        Checks (in order): halt flag → position size → daily spend circuit breaker.
        """
        if self._is_halted():
            return False

        cost = intent.target_qty * (intent.price_cents / 100.0)

        if cost > self.max_position_size:
            logger.warning(
                "Position size limit exceeded: $%.2f > $%.2f for %s",
                cost, self.max_position_size, intent.ticker,
            )
            return False

        # Daily spend circuit breaker — uses executions table as source of truth
        daily_spent = self.db.get_daily_realized_pnl(env_mode)
        if daily_spent >= self.daily_loss_limit:
            self._set_halt(
                f"Daily spend limit ${self.daily_loss_limit:.2f} reached (spent ${daily_spent:.2f})"
            )
            return False

        logger.info(
            "Preflight approved: %s qty=%d @ %dc (daily_spent=$%.2f)",
            intent.ticker, intent.target_qty, intent.price_cents, daily_spent,
        )
        return True
