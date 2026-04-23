"""
One-time bootstrap: ingest 90 days of historical weather actuals for all cities.

Run once before starting live trading to seed the AR(1) residuals and
weather_actuals tables so σ MLE and φ estimation have data from day one.

Usage:
    python scripts/bootstrap_historical.py
    python scripts/bootstrap_historical.py --days 180
"""
import asyncio
import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db.dwtrader import DWTraderDB
from src.ingest.weather import WeatherIngestor
from src.config.env import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] - %(message)s")
logger = logging.getLogger("bootstrap")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90,
                        help="How many days of history to ingest (default: 90)")
    args = parser.parse_args()

    db = DWTraderDB()
    ingestor = WeatherIngestor(db)

    end_date = (date.today() - timedelta(days=1)).isoformat()
    start_date = (date.today() - timedelta(days=args.days)).isoformat()

    cities = list(Config.CITY_COORDS.keys())
    logger.info("Bootstrapping %d days of historical weather for %d cities (%s → %s)",
                args.days, len(cities), start_date, end_date)

    for i, city in enumerate(cities, 1):
        logger.info("[%d/%d] Ingesting %s ...", i, len(cities), city)
        try:
            await ingestor.ingest_historical_data(city, start_date, end_date)
        except Exception as e:
            logger.error("Failed for %s: %s", city, e)
        await asyncio.sleep(0.5)  # be polite to Open-Meteo

    logger.info("Bootstrap complete. Checking row counts ...")
    with db.get_connection() as conn:
        actuals = conn.execute("SELECT COUNT(*) FROM weather_actuals").fetchone()[0]
        weather = conn.execute("SELECT COUNT(*) FROM weather_data WHERE is_historical=1").fetchone()[0]
    logger.info("weather_actuals: %d rows | weather_data (historical): %d rows", actuals, weather)


if __name__ == "__main__":
    asyncio.run(main())
