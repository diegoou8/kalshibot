import asyncio
import logging
from src.services.kalshi_client import KalshiClient
from src.db.dwtrader import DWTraderDB
from src.ingest.ws_client import IngestionPipeline, KalshiWebSocketClient
from src.ingest.weather import WeatherIngestor
from src.config.env import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] - %(name)s - %(message)s", force=True)
logger = logging.getLogger(__name__)

async def run_weather_ingest(db: DWTraderDB):
    weather = WeatherIngestor(db)
    while True:
        try:
            logger.info("🌤 Starting periodic weather ingestion...")
            cities = Config.CITY_COORDS.keys() if hasattr(Config, 'CITY_COORDS') else ["NEW YORK", "CHICAGO", "LOS ANGELES"]
            for city in cities:
                await weather.ingest_forecast_data(city)
            logger.info("✅ Weather ingestion loop completed.")
        except Exception as e:
            logger.error(f"Weather ingestion error: {e}")
        await asyncio.sleep(3600)  # Hourly update

async def run_websocket_ingest(db: DWTraderDB):
    client = KalshiClient()
    pipeline = IngestionPipeline(db)
    uri = "wss://demo-api.kalshi.co/trade-api/ws/v2"
    ws = KalshiWebSocketClient(pipeline, uri, client)

    logger.info("🔌 Connecting to Kalshi WebSocket for market data ingestion...")
    while True:
        try:
            await ws.connect()
        except Exception as e:
            logger.error(f"WebSocket error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)

async def bootstrap_historical_once(db: DWTraderDB):
    """
    Seed weather_actuals and ar1_residuals with 90 days of history on first run.
    Skips silently if data already exists.
    """
    with db.get_connection() as conn:
        n = conn.execute("SELECT COUNT(*) FROM weather_actuals").fetchone()[0]
    if n > 0:
        logger.info("Historical data already seeded (%d rows). Skipping bootstrap.", n)
        return

    logger.info("Seeding historical weather data (90 days) — this runs once...")
    weather = WeatherIngestor(db)
    from datetime import date, timedelta
    end_date   = (date.today() - timedelta(days=1)).isoformat()
    start_date = (date.today() - timedelta(days=90)).isoformat()
    for city in Config.CITY_COORDS.keys():
        try:
            await weather.ingest_historical_data(city, start_date, end_date)
        except Exception as e:
            logger.warning("Bootstrap failed for %s: %s", city, e)
        await asyncio.sleep(0.3)
    logger.info("Historical bootstrap complete.")


async def main():
    logger.info("Starting Layer 1 Ingestion System (Kalshi WebSocket + Open-Meteo)")
    db = DWTraderDB()

    # Seed historical data once so AR(1) / σ calibration has data from day one
    await bootstrap_historical_once(db)

    # Run both the websocket market ingest and weather polling concurrently
    await asyncio.gather(
        run_websocket_ingest(db),
        run_weather_ingest(db)
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down Layer 1 ingestion.")
