"""Quick script to exercise the Kalshi websocket and verify schema alignment."""
import asyncio
import json
import logging

import websockets

from src.services.kalshi_client import KalshiClient
from src.db.dwtrader import DWTraderDB
from src.ingest.ws_client import IngestionPipeline, KalshiWebSocketClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] - %(message)s")
logger = logging.getLogger(__name__)


def simple_local_test():
    """Simulate a market_update event and ensure persistence"""
    db = DWTraderDB()
    pipeline = IngestionPipeline(db)
    # create a fake event resembling a websocket snapshot
    event = {
        "type": "market_snapshot",
        "ticker": "TEST-XYZ",
        "yes_bid": 20,
        "yes_ask": 25,
        "no_bid": 75,
        "no_ask": 80,
        "volume": 123,
        "environment": "PAPER"
    }
    asyncio.run(pipeline.handle_event(event))
    logger.info("Sample event persisted; check DB scans table")


async def live_connect():
    client = KalshiClient()
    db = DWTraderDB()
    pipeline = IngestionPipeline(db)
    uri = "wss://demo-api.kalshi.co/trade-api/ws/v2"
    ws = KalshiWebSocketClient(pipeline, uri, client)

    try:
        await ws.connect()
    except KeyboardInterrupt:
        logger.info("Interrupted, closing")


if __name__ == "__main__":
    # run the local fake data test first
    simple_local_test()
    # uncomment the next line to attempt a real connection (requires API key file/etc.)
    # asyncio.run(live_connect())
