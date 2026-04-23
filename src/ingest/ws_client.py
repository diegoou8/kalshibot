import asyncio
import json
import logging
from typing import Any, Dict, List, Set

from ..db.dwtrader import DWTraderDB
from ..config.env import Config

logger = logging.getLogger(__name__)


class IngestionPipeline:
    """Simple router that takes raw events and persists them to the database.

    The purpose of this class is to isolate the rest of the system from the
    transport (HTTP poll versus WebSocket).  Any component that receives a
    market/weather/event message should call ``pipeline.handle_event()``; the
    pipeline will examine ``event['type']`` and dispatch to the appropriate
    helper.
    """

    def __init__(self, db: DWTraderDB):
        self.db = db

    async def handle_event(self, event: Dict[str, Any]) -> None:
        typ = event.get("type")
        if typ == "weather_update":
            await self._persist_weather(event)
        elif typ == "market_snapshot":
            await self._persist_market(event)
        elif typ == "orderbook_delta":
            await self._persist_orderbook(event)
        else:
            # other events (orderbook, trade, etc.) can be added later
            logger.debug("ignoring unrecognised event type %s", typ)

    async def _persist_weather(self, ev: Dict[str, Any]) -> None:
        # expected fields: city, target_date, max_temp_f, precip_inch, is_historical, hour?
        try:
            self.db.log_weather(
                city=ev["city"],
                target_date=ev["target_date"],
                max_temp_f=ev.get("max_temp_f", 0.0),
                precip_inch=ev.get("precip_inch", 0.0),
                is_historical=ev.get("is_historical", False),
                hour=ev.get("hour"),
            )
        except Exception as e:
            logger.error("failed to persist weather event %s: %s", ev, e)

    async def _persist_market(self, ev: Dict[str, Any]) -> None:
        # this is a very thin wrapper; existing market logging logic still
        # lives in trade_logger/decision pipeline, but we persist the raw
        # snapshot so that a perturbation from the WebSocket can later be
        # replayed in tests.
        try:
            # event may already contain all relevant fields; if not, you can
            # modify the mapping here accordingly.
            yes_ask = ev.get("yes_ask")
            if yes_ask is None:
                yes_ask = 100
                
            yes_bid = ev.get("yes_bid")
            if yes_bid is None:
                yes_bid = 0
                
            volume = ev.get("volume")
            if volume is None:
                volume = 0
                
            self.db.log_scan(
                ticker=ev.get("ticker"),
                market_prob=yes_ask / 100.0,
                ml_prob=0.0,
                best_bid=yes_bid,
                best_ask=yes_ask,
                spread=(yes_ask - yes_bid),
                volume=volume,
                environment=ev.get("environment", "PAPER"),
            )
        except Exception as e:
            logger.error("failed to persist market snapshot %s: %s", ev, e)

    async def _persist_orderbook(self, ev: Dict[str, Any]) -> None:
        """
        Persist raw orderbook events as JSON so they can be replayed later for
        microstructure / toxicity analysis.
        """
        try:
            ticker = ev.get("ticker")
            payload = ev.get("payload")
            if not ticker or payload is None:
                return
            self.db.log_orderbook_event(
                ticker=ticker,
                msg_type="orderbook_delta",
                payload=json.dumps(payload),
                environment=ev.get("environment", "PAPER"),
            )
        except Exception as e:
            logger.error("failed to persist orderbook event %s: %s", ev, e)


class KalshiWebSocketClient:
    """Minimal asynchronous websocket wrapper for Kalshi.

    This client keeps a single connection open to the provided ``uri`` and
    pushes every JSON message it receives into the supplied ``pipeline``.
    It also handles the signing headers required by the Demo/Live API.

    Incoming messages from Kalshi usually have the form::

        {"type": "snapshot", "market": {<market fields>}}

    or similar; we unwrap the nested ``market`` key and repackage the data as
    ``event[type='market_snapshot']`` so that downstream code can treat polling
    and streaming identically.
    """

    def __init__(self, pipeline: IngestionPipeline, uri: str, client=None):
        self.pipeline = pipeline
        self.uri = uri
        self.client = client  # optional KalshiClient instance used for signing
        self._running = False
        self._weather_tickers: Set[str] = set()

    @staticmethod
    def _is_weather_market(m: Dict[str, Any]) -> bool:
        """
        Identify weather markets from REST metadata using explicit categories
        only, to avoid accidentally pulling in sports/FX that mention city
        names in titles.
        """
        categories = [c.upper() for c in (m.get("categories") or [])]
        return any("WEATHER" in c for c in categories)

    async def connect(self) -> None:
        """Establish the connection and start dispatching messages.

        Uses Kalshi's documented websocket flow:
          - authenticate via headers
          - subscribe to the ``ticker`` channel
          - map incoming ticker messages into our generic event format
            understood by ``IngestionPipeline``.
        """
        import websockets  # import here so the dependency is obvious

        bootstrapped = False

        self._running = True
        while self._running:
            try:
                if self.client:
                    try:
                        weather_markets = [m for m in await self.client.get_weather_markets() if m.get("ticker")]
                        self._weather_tickers = {m["ticker"] for m in weather_markets}
                        logger.info(
                            "identified %d weather markets for websocket ingestion",
                            len(self._weather_tickers),
                        )

                        # Bootstrap: write a baseline scan row for each weather market
                        if not bootstrapped:
                            for m in weather_markets:
                                ev = {
                                    "type": "market_snapshot",
                                    "ticker": m.get("ticker"),
                                    "yes_bid": m.get("yes_bid", 0),
                                    "yes_ask": m.get("yes_ask", 100),
                                    "volume": m.get("volume", 0),
                                    "environment": "PAPER",
                                }
                                await self.pipeline.handle_event(ev)
                            if weather_markets:
                                logger.info("bootstrapped %d weather markets into scans", len(weather_markets))
                            bootstrapped = True
                    except Exception as e:
                        logger.error(f"Failed to discover weather markets for websocket: {e}")
                        self._weather_tickers = set()

                logger.info("connecting to websocket %s", self.uri)
                
                headers = None
                if self.client:
                    headers = self.client._get_headers("GET", "/trade-api/ws/v2")
                    
                async with websockets.connect(self.uri, additional_headers=headers) as ws:
                    # Subscribe to ticker updates (we'll filter to weather markets client-side).
                    ticker_sub = {
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["ticker"]
                        }
                    }
                    await ws.send(json.dumps(ticker_sub))
                    logger.info("subscribed to 'ticker' channel")

                    # Subscribe to orderbook deltas for weather markets only.
                    if self._weather_tickers:
                        ob_sub = {
                            "id": 2,
                            "cmd": "subscribe",
                            "params": {
                                "channels": ["orderbook_delta"],
                                "market_tickers": list(self._weather_tickers),
                            },
                        }
                        await ws.send(json.dumps(ob_sub))
                        logger.info(
                            "subscribed to 'orderbook_delta' for %d weather markets",
                            len(self._weather_tickers),
                        )

                    async def refresh_task():
                        while self._running:
                            await asyncio.sleep(3600)  # Refresh every hour
                            if not self.client:
                                continue
                            try:
                                logger.info("Background refresh of weather markets...")
                                w_mkts = [m for m in await self.client.get_weather_markets() if m.get("ticker")]
                                new_tickers = {m["ticker"] for m in w_mkts}
                                added_tickers = new_tickers - self._weather_tickers
                                if added_tickers:
                                    logger.info(f"Discovered {len(added_tickers)} new weather markets: {added_tickers}")
                                    self._weather_tickers.update(added_tickers)
                                    await ws.send(json.dumps({
                                        "id": 3,
                                        "cmd": "subscribe",
                                        "params": {
                                            "channels": ["orderbook_delta"],
                                            "market_tickers": list(added_tickers),
                                        }
                                    }))
                            except Exception as e:
                                logger.error(f"Error in background market refresh: {e}")

                    refresher = asyncio.create_task(refresh_task())

                    try:
                        async for msg in ws:
                                try:
                                    data = json.loads(msg)
                                    msg_type = data.get("type")

                                    if msg_type == "ticker":
                                        m = data.get("msg", {})
                                        market_ticker = m.get("market_ticker")
                                        # If we discovered weather tickers, ignore non-weather.
                                        if self._weather_tickers and market_ticker not in self._weather_tickers:
                                            continue
                                        # Normalise into our generic market_snapshot event
                                        ev = {
                                            "type": "market_snapshot",
                                            "ticker": market_ticker,
                                            "yes_bid": m.get("yes_bid"),
                                            "yes_ask": m.get("yes_ask"),
                                            "volume": m.get("volume", 0),
                                            # environment is stored so DWTrader can track SHADOW/PAPER/LIVE;
                                            # for demo streaming we treat it as PAPER.
                                            "environment": "PAPER",
                                        }
                                        await self.pipeline.handle_event(ev)
                                    elif msg_type in ("orderbook_snapshot", "orderbook_delta"):
                                        m = data.get("msg", {}) or {}
                                        market_ticker = m.get("market_ticker")
                                        if self._weather_tickers and market_ticker not in self._weather_tickers:
                                            continue
                                        ev = {
                                            "type": "orderbook_delta",
                                            "ticker": market_ticker,
                                            "payload": m,
                                            "environment": "PAPER",
                                        }
                                        await self.pipeline.handle_event(ev)
                                    elif msg_type == "error":
                                        err = data.get("msg", {}) or {}
                                        logger.error(
                                            "WebSocket error %s: %s",
                                            err.get("code"),
                                            err.get("msg"),
                                        )
                                    else:
                                        # orderbook_snapshot / orderbook_delta / other types
                                        logger.debug("ignoring ws message type %s", msg_type)
                                except json.JSONDecodeError:
                                    logger.warning("received non-json message: %s", msg)
                                except Exception as e:
                                    logger.error("error handling ws message %s: %s", msg, e)
                    finally:
                        refresher.cancel()
            except Exception as conn_exc:
                logger.warning("websocket connection failed: %s, retrying in 5s", conn_exc)
                await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False
