import logging
import re as _re
from pathlib import Path
from typing import Optional

from ..config.env import Config
from ..db.dwtrader import DWTraderDB
from ..decision.engine import TradeIntent

logger = logging.getLogger(__name__)

_HALT_FLAG = Path(__file__).resolve().parents[2] / "data" / "halt.flag"


def _ticker_settle_date(ticker: str) -> Optional[str]:
    """Parse Kalshi ticker → ISO settlement date, e.g. 'KXHIGHLAX-26APR28-T64' → '2026-04-28'."""
    from datetime import datetime as _dt
    m = _re.match(r"KX(?:HIGH|TEMP)[A-Z]+-(\d{2}[A-Z]{3}\d{2})", ticker, _re.IGNORECASE)
    if not m:
        return None
    try:
        return _dt.strptime(m.group(1).upper(), "%y%b%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


class RiskManager:
    """
    Guardrails and idempotency checks prior to execution.
    Checks: halt flag, position size limit, daily spend limit.
    """

    # Hard cap: no more than this many contracts open per city+date slot.
    MAX_CONTRACTS_PER_SLOT = 4
    # Hard cap: no more than this many distinct open positions per city+date slot.
    MAX_POSITIONS_PER_SLOT = 2

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

        # Concentration cap: max MAX_CONTRACTS_PER_SLOT open contracts per city+date
        m = _re.match(r"KX(?:HIGH|TEMP)([A-Z]+)-(\d{2}[A-Z]{3}\d{2})", intent.ticker, _re.IGNORECASE)
        if m:
            city = m.group(1).upper()
            from datetime import datetime as _dt
            try:
                settle_date = _dt.strptime(m.group(2).upper(), "%y%b%d").strftime("%Y-%m-%d")
            except ValueError:
                settle_date = None
            if settle_date:
                open_pos = self.db.get_open_positions(env_mode)
                slot_positions = [
                    p for p in open_pos
                    if _re.search(rf"KX(?:HIGH|TEMP){city}-", p["ticker"], _re.IGNORECASE)
                    and _ticker_settle_date(p["ticker"]) == settle_date
                ]
                # Position-count cap: max 2 distinct positions per city+date
                if len(slot_positions) >= self.MAX_POSITIONS_PER_SLOT:
                    logger.warning(
                        "BLOCKED_CITY_CONCENTRATION: %s already has %d open position(s) on %s (max=%d)",
                        intent.ticker, len(slot_positions), settle_date, self.MAX_POSITIONS_PER_SLOT,
                    )
                    return False
                # Contract-quantity cap: max 4 contracts per city+date
                slot_qty = sum(p["qty"] for p in slot_positions)
                if slot_qty + intent.target_qty > self.MAX_CONTRACTS_PER_SLOT:
                    logger.warning(
                        "BLOCKED_CITY_CONCENTRATION: %s slot already has %d contracts (cap=%d)",
                        f"{city}_{settle_date}", slot_qty, self.MAX_CONTRACTS_PER_SLOT,
                    )
                    return False

                # Max gross contracts across ALL tickers settling on the same date
                from src.config.env import Config as _Cfg
                daily_qty = sum(
                    p["qty"] for p in open_pos
                    if _ticker_settle_date(p["ticker"]) == settle_date
                )
                if daily_qty + intent.target_qty > _Cfg.MAX_DAILY_GROSS_CONTRACTS:
                    logger.warning(
                        "Daily gross cap: %s already has %d contracts on %s (cap=%d)",
                        intent.ticker, daily_qty, settle_date, _Cfg.MAX_DAILY_GROSS_CONTRACTS,
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
