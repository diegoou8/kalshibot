import aiohttp
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from functools import lru_cache
import asyncio

from ..config.env import Config
from ..db.dwtrader import DWTraderDB

logger = logging.getLogger(__name__)

_TIMEOUT      = aiohttp.ClientTimeout(total=30)
_MAX_RETRIES  = 3
_BACKOFF_SECS = (2, 4, 8)   # wait before retry 1, 2, 3

# Exceptions that are transient and worth retrying
_RETRYABLE = (
    aiohttp.ServerTimeoutError,
    aiohttp.ServerDisconnectedError,
    aiohttp.ClientConnectorError,
    asyncio.TimeoutError,
)


class WeatherIngestor:
    def __init__(self, db: DWTraderDB):
        self.base_url_forecast = "https://api.open-meteo.com/v1/forecast"
        self.base_url_history  = "https://archive-api.open-meteo.com/v1/archive"
        self.city_coords       = Config.CITY_COORDS
        self.db                = db
        self._session: Optional[aiohttp.ClientSession] = None

    def _get_session(self) -> aiohttp.ClientSession:
        """Return the shared session, creating it lazily or after it was closed."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _fetch_with_retry(self, url: str, params: dict, city: str) -> dict:
        """GET url with params, retrying up to _MAX_RETRIES times with exponential backoff."""
        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                session = self._get_session()
                async with session.get(url, params=params) as resp:
                    if resp.status >= 500:
                        raise aiohttp.ClientResponseError(
                            resp.request_info, resp.history, status=resp.status
                        )
                    resp.raise_for_status()
                    return await resp.json()
            except _RETRYABLE + (aiohttp.ClientResponseError,) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    wait = _BACKOFF_SECS[attempt]
                    logger.warning(
                        "Weather fetch attempt %d/%d failed for %s (%s) — retrying in %ds",
                        attempt + 1, _MAX_RETRIES + 1, city, exc, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error("Weather API error for %s: %s", city, exc)
        raise last_exc

    async def ingest_historical_data(self, city: str, start_date: str, end_date: str):
        """
        Ingest historical daily and hourly weather data for model training context.
        start_date, end_date format: 'YYYY-MM-DD'
        """
        city_upper = city.upper()
        if city_upper not in self.city_coords:
            logger.warning("City coords not found for: %s", city)
            return

        lat, lon = self.city_coords[city_upper]

        try:
            params = {
                "latitude":           lat,
                "longitude":          lon,
                "start_date":         start_date,
                "end_date":           end_date,
                "daily":              ["temperature_2m_max", "precipitation_sum"],
                "hourly":             ["temperature_2m", "precipitation"],
                "temperature_unit":   "fahrenheit",
                "precipitation_unit": "inch",
                "timezone":           "auto",
            }

            logger.info("Fetching historical weather for %s (%s to %s)...", city, start_date, end_date)
            data = await self._fetch_with_retry(self.base_url_history, params, city_upper)

            # Daily history → weather_actuals + legacy weather_data
            daily     = data.get("daily", {})
            dates     = daily.get("time", [])
            max_temps = daily.get("temperature_2m_max", [])
            precips   = daily.get("precipitation_sum", [])

            for i, date_str in enumerate(dates):
                if max_temps[i] is not None:
                    self.db.log_weather_actual(
                        city=city_upper, target_date=date_str,
                        actual_temp_f=max_temps[i],
                    )
                    self.db.log_weather(
                        city=city_upper, target_date=date_str,
                        max_temp_f=max_temps[i],
                        precip_inch=precips[i] if precips[i] is not None else 0.0,
                        is_historical=True,
                    )

            # Hourly history
            hourly   = data.get("hourly", {})
            h_times  = hourly.get("time", [])
            h_temps  = hourly.get("temperature_2m", [])
            h_precips = hourly.get("precipitation", [])

            for i, time_str in enumerate(h_times):
                if h_temps[i] is not None:
                    date_part, time_part = time_str.split("T")
                    hour_val = int(time_part.split(":")[0])
                    self.db.log_weather_actual(
                        city=city_upper, target_date=date_part,
                        actual_temp_f=h_temps[i], hour=hour_val,
                    )
                    self.db.log_weather(
                        city=city_upper, target_date=date_part, hour=hour_val,
                        max_temp_f=h_temps[i],
                        precip_inch=h_precips[i] if h_precips[i] is not None else 0.0,
                        is_historical=True,
                    )

            logger.info("Historical weather logged for %s.", city)

        except Exception as exc:
            logger.error("Failed to ingest historical data for %s: %s", city, exc)

    async def ingest_forecast_data(self, city: str) -> None:
        """Fetch 7-day forecast for one city and write it to the DB."""
        city_upper = city.upper()
        if city_upper not in self.city_coords:
            return

        lat, lon = self.city_coords[city_upper]

        try:
            params = {
                "latitude":           lat,
                "longitude":          lon,
                "daily":              ["temperature_2m_max", "precipitation_sum"],
                "hourly":             ["temperature_2m", "precipitation"],
                "temperature_unit":   "fahrenheit",
                "precipitation_unit": "inch",
                "timezone":           "auto",
                "past_days":          1,
                "forecast_days":      7,
            }

            data = await self._fetch_with_retry(self.base_url_forecast, params, city_upper)

            # Daily forecasts
            daily     = data.get("daily", {})
            dates     = daily.get("time", [])
            max_temps = daily.get("temperature_2m_max", [])
            precips   = daily.get("precipitation_sum", [])

            for i, date_str in enumerate(dates):
                if max_temps[i] is not None:
                    self.db.log_weather(
                        city=city_upper, target_date=date_str,
                        max_temp_f=max_temps[i],
                        precip_inch=precips[i] if precips[i] else 0,
                        is_historical=False,
                    )

            # Hourly forecasts
            hourly    = data.get("hourly", {})
            h_times   = hourly.get("time", [])
            h_temps   = hourly.get("temperature_2m", [])
            h_precips = hourly.get("precipitation", [])

            for i, time_str in enumerate(h_times):
                if h_temps[i] is not None:
                    date_part, time_part = time_str.split("T")
                    hour_val = int(time_part.split(":")[0])
                    self.db.log_weather(
                        city=city_upper, target_date=date_part, hour=hour_val,
                        max_temp_f=h_temps[i],
                        precip_inch=h_precips[i] if h_precips[i] else 0,
                        is_historical=False,
                    )

            logger.info("Forecast updated for %s.", city_upper)

        except Exception as exc:
            logger.error("Weather API error for %s: %s", city_upper, exc)
