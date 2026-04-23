import asyncio
import logging
import time
from typing import Dict, Any

from ..services.kalshi_client import KalshiClient
from ..decision.engine import TradeIntent

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 2
_POLL_TIMEOUT_S  = 30


class ExecutionManager:
    """
    Routes orders to the exchange with a pre-flight orderbook depth check
    and post-submit fill polling.

    Flow:
      1. Depth check — warn if best ask has drifted past our limit price.
      2. Submit limit order with a 10-second IOC expiry.
      3. If order is immediately executed: return the fill.
      4. If order is resting: poll every 2s for up to 30s waiting for a fill.
      5. If still resting after 30s: cancel the order, return status='timeout'.
    """

    def __init__(self, client: KalshiClient):
        self.client = client

    async def execute(self, intent: TradeIntent, client_order_id: str) -> Dict[str, Any]:
        logger.info(
            "⏳ Executing Snipe for %s: %dx @ %dc",
            intent.ticker, intent.target_qty, intent.price_cents,
        )

        # ── Pre-flight depth check ────────────────────────────────────────────
        ob = await self.client.get_order_book(intent.ticker)
        best_ask = 100
        yes_asks = ob.get("yes_asks", []) or ob.get("asks", [])
        if yes_asks and isinstance(yes_asks, list):
            try:
                clean = [(int(p), int(q)) for p, q in yes_asks
                         if isinstance(p, (int, str)) and isinstance(q, (int, str))]
                if clean:
                    best_ask = min(p for p, q in clean)
            except Exception as exc:
                logger.warning("Failed to parse orderbook asks: %s | %s", yes_asks, exc)

        if best_ask > intent.price_cents:
            logger.warning(
                "⚠️ Price Drift: Best Ask %dc > Limit %dc. Order may rest.",
                best_ask, intent.price_cents,
            )

        # ── Submit ────────────────────────────────────────────────────────────
        expiration_ts = int(time.time()) + 10
        result = await self.client.submit_order(
            ticker=intent.ticker,
            side=intent.side,
            action="buy",
            count=intent.target_qty,
            price_cents=intent.price_cents,
            client_order_id=client_order_id,
            expiration_ts=expiration_ts,
        )

        if result.get("status") == "error":
            return result

        order_data = result.get("order", {})
        order_id   = order_data.get("order_id")

        # Immediately executed — done
        if order_data.get("status") == "executed":
            logger.info("📡 Exchange Response: %s", result)
            return result

        # ── Poll for fill ─────────────────────────────────────────────────────
        if order_id and order_data.get("status") in ("resting", "pending", "open"):
            logger.info(
                "📡 Order %s resting — polling for fill (timeout %ds)...",
                order_id, _POLL_TIMEOUT_S,
            )
            elapsed = 0
            while elapsed < _POLL_TIMEOUT_S:
                await asyncio.sleep(_POLL_INTERVAL_S)
                elapsed += _POLL_INTERVAL_S

                order = await self.client.get_order(order_id)
                status = order.get("status", "")

                if status == "executed":
                    logger.info(
                        "✅ FILLED %s after %ds polling", intent.ticker, elapsed
                    )
                    result["order"] = order
                    return result

                if status == "canceled":
                    logger.info("Order %s canceled by exchange", order_id)
                    return {"status": "canceled", "order": order}

                logger.debug("Polling %s: status=%s elapsed=%ds", order_id, status, elapsed)

            # Timeout — cancel the resting order to avoid stale exposure
            logger.warning(
                "⏰ Poll timeout (%ds) for %s — canceling resting order",
                _POLL_TIMEOUT_S, order_id,
            )
            canceled = await self.client.cancel_order(order_id)
            logger.info("Cancel %s: %s", order_id, "OK" if canceled else "failed")
            return {"status": "timeout", "order": order_data}

        # Order in unknown state — return as-is
        logger.info("📡 Exchange Response: %s", result)
        return result

    async def close_position(
        self,
        ticker: str,
        side: str,
        qty: int,
        bid_cents: int,
        client_order_id: str,
    ) -> Dict[str, Any]:
        """
        Exit qty contracts of an existing position by selling at bid_cents.
        Mirrors execute() but uses action='sell' and skips the depth-check
        (we're the seller, not the buyer — price drift works in our favour).
        """
        logger.info(
            "📤 Closing %s %s qty=%d @ %dc",
            ticker, side, qty, bid_cents,
        )

        expiration_ts = int(time.time()) + 10
        result = await self.client.submit_order(
            ticker=ticker,
            side=side,
            action="sell",
            count=qty,
            price_cents=bid_cents,
            client_order_id=client_order_id,
            expiration_ts=expiration_ts,
        )

        if result.get("status") == "error":
            return result

        order_data = result.get("order", {})
        order_id   = order_data.get("order_id")

        if order_data.get("status") == "executed":
            logger.info("📤 SOLD %s immediately @ %dc", ticker, bid_cents)
            return result

        if order_id and order_data.get("status") in ("resting", "pending", "open"):
            logger.info(
                "📤 Sell order %s resting — polling (timeout %ds)...",
                order_id, _POLL_TIMEOUT_S,
            )
            elapsed = 0
            while elapsed < _POLL_TIMEOUT_S:
                await asyncio.sleep(_POLL_INTERVAL_S)
                elapsed += _POLL_INTERVAL_S
                order = await self.client.get_order(order_id)
                status = order.get("status", "")
                if status == "executed":
                    logger.info("📤 SOLD %s after %ds", ticker, elapsed)
                    result["order"] = order
                    return result
                if status == "canceled":
                    return {"status": "canceled", "order": order}

            await self.client.cancel_order(order_id)
            logger.warning("📤 Sell order for %s timed out and was canceled", ticker)
            return {"status": "timeout", "order": order_data}

        return result
