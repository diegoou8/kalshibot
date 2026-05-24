import logging
from typing import Dict, Any, Optional

from ..db.dwtrader import DWTraderDB
from ..decision.engine import TradeIntent
from ..config.env import Config

logger = logging.getLogger(__name__)

class TradeLogger:
    """
    Wraps DB calls to map the business logic of our structured Execution Loop
    directly into our SQLite database. Isolates DB semantics from trading pipeline.
    """
    def __init__(self, db: DWTraderDB):
        self.db = db

    def log_scan_step(self, market: Dict[str, Any], env_mode: str) -> Optional[int]:
        ticker = market.get('ticker')
        yes_ask = market.get('yes_ask', 100)
        yes_bid = market.get('yes_bid', 0)
        spread = yes_ask - yes_bid

        scan_id = self.db.log_scan(
            ticker=ticker,
            market_prob=yes_ask / 100.0,
            ml_prob=0.0,
            best_bid=yes_bid,
            best_ask=yes_ask,
            spread=spread,
            volume=market.get('volume', 0),
            environment=env_mode
        )
        return scan_id

    def log_decision_step(self, intent: Optional[TradeIntent], scan_id: int, env_mode: str) -> Optional[int]:
        if not intent:
            decision = "SKIP"
            arb_signal = "none"
            ev = 0.0
            kelly = 0.0
            ml_prob = 0.0
            risk_score = 0.0
        else:
            decision = "SUBMIT"
            arb_signal = intent.reason
            ev = intent.expected_value
            kelly = intent.kelly_fraction
            ml_prob = intent.confidence
            # Effective position cost as fraction of max position size — non-zero for real trades.
            risk_score = intent.kelly_fraction * intent.price_cents / 100.0

        return self.db.log_decision(
            scan_id=scan_id,
            expected_value=ev,
            kelly_fraction=kelly,
            risk_score=risk_score,
            ml_prob=ml_prob,
            arb_signal=arb_signal,
            decision=decision,
            environment=env_mode
        )

    def log_intent_step(self, intent: TradeIntent, env_mode: str) -> Optional[int]:
        return self.db.log_intent(
            scan_id=intent.scan_id,
            ticker=intent.ticker,
            side=intent.side,
            expected_price=intent.price_cents,
            target_qty=intent.target_qty,
            status="PENDING",
            environment=env_mode
        )

    def log_order_result(self, intent: TradeIntent, intent_id: int, exchange_order_id: str, status: str, env_mode: str) -> Optional[int]:
        return self.db.log_order(
            intent_id=intent_id,
            exchange_order_id=exchange_order_id,
            ticker=intent.ticker,
            side=intent.side,
            price=intent.price_cents,
            qty=intent.target_qty,
            order_type="limit",
            status=status,
            environment=env_mode,
            gumbel_mode=Config.GUMBEL_MODE,
        )

    def log_prediction(self, ticker: str, side: str, predicted_p: float,
                       city: Optional[str] = None, tau_hrs: Optional[float] = None,
                       horizon_bin: Optional[str] = None, sigma: Optional[float] = None,
                       ar1_correction: Optional[float] = None) -> Optional[int]:
        """Record brain's P(YES) at intent time for later Brier scoring."""
        from datetime import date
        return self.db.log_prediction(
            ticker=ticker,
            trade_date=str(date.today()),
            side=side,
            predicted_p=predicted_p,
            city=city,
            horizon_hrs=tau_hrs,
            horizon_bin=horizon_bin,
            sigma=sigma,
            ar1_correction=ar1_correction,
        )

    def log_execution_fill(
        self,
        order_id: int,
        exchange_trade_id: str,
        ticker: str,
        side: str,
        price: int,
        qty: int,
        env_mode: str,
        scan_mid_cents: Optional[int] = None,
        predicted_p: Optional[float] = None,
        market_implied_p: Optional[float] = None,
        city: Optional[str] = None,
        horizon_bin: Optional[str] = None,
        expected_value_cents: Optional[float] = None,
        fees_cents: Optional[float] = None,
    ) -> Optional[int]:
        """
        Log a buy fill: write execution row, then write trade_attribution row.
        lvr_cents is always computed when scan_mid_cents is available.
        Returns execution_id, or None on duplicate/error.
        """
        lvr_cents = price - scan_mid_cents if scan_mid_cents is not None else None
        execution_id = self.db.log_execution(
            order_id=order_id,
            exchange_trade_id=exchange_trade_id,
            ticker=ticker,
            side=side,
            price=price,
            qty=qty,
            environment=env_mode,
            lvr_cents=lvr_cents,
            gumbel_mode=Config.GUMBEL_MODE,
        )

        if execution_id is not None:
            slippage = lvr_cents  # fill_price - mid: positive = favourable for buyer
            attribution_id = self.db.log_trade_attribution(
                ticker=ticker,
                execution_id=execution_id,
                city=city,
                side=side,
                horizon_bin=horizon_bin,
                fill_price_cents=price,
                mid_at_fill_cents=scan_mid_cents,
                predicted_p=predicted_p,
                market_implied_p=market_implied_p,
                expected_value_cents=expected_value_cents,
                slippage_cents=slippage,
                fees_cents=fees_cents,
            )
            # After trade_attribution insert
            if execution_id is not None and attribution_id is None:
                logger.error("ATTRIBUTION_WRITE_FAILED: %s exec_id=%d", ticker, execution_id)
            if scan_mid_cents is not None and lvr_cents is None:
                logger.error("LVR_NULL_UNEXPECTED: %s", ticker)
        return execution_id
