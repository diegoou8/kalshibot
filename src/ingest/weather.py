import aiohttp
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from functools import lru_cache
import asyncio

from ..config.env import Config
from ..db.dwtrader import DWTraderDB

logger = logging.getLogger(__name__)

class WeatherIngestor:
    def __init__(self, db: DWTraderDB):
        self.base_url_forecast = "https://api.open-meteo.com/v1/forecast"
        self.base_url_history = "https://archive-api.open-meteo.com/v1/archive"
        self.city_coords = Config.CITY_COORDS
        self.db = db

    async def ingest_historical_data(self, city: str, start_date: str, end_date: str):
        """
        Ingest historical daily and hourly weather data for model training context.
        start_date, end_date format: 'YYYY-MM-DD'
        """
        city_upper = city.upper()
        if city_upper not in self.city_coords:
            logger.warning(f"City coords not found for: {city}")
            return
            
        lat, lon = self.city_coords[city_upper]
        
        try:
            # We fetch both daily max temp/precip and hourly data
            params = {
                "latitude": lat,
                "longitude": lon,
                "start_date": start_date,
                "end_date": end_date,
                "daily": ["temperature_2m_max", "precipitation_sum"],
                "hourly": ["temperature_2m", "precipitation"],
                "temperature_unit": "fahrenheit",
                "precipitation_unit": "inch",
                "timezone": "auto"
            }
            
            logger.info(f"⏳ Fetching historical weather data for {city} ({start_date} to {end_date})...")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(self.base_url_history, params=params) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            
            # Log Daily History — into weather_actuals (confirmed, separate from forecasts)
            daily = data.get('daily', {})
            dates = daily.get('time', [])
            max_temps = daily.get('temperature_2m_max', [])
            precips = daily.get('precipitation_sum', [])

            for i, date_str in enumerate(dates):
                if max_temps[i] is not None:
                    self.db.log_weather_actual(
                        city=city_upper, target_date=date_str,
                        actual_temp_f=max_temps[i],
                    )
                    # Also keep the legacy weather_data record for compatibility
                    self.db.log_weather(
                        city=city_upper, target_date=date_str,
                        max_temp_f=max_temps[i],
                        precip_inch=precips[i] if precips[i] is not None else 0.0,
                        is_historical=True,
                    )

            # Log Hourly History
            hourly = data.get('hourly', {})
            h_times = hourly.get('time', [])
            h_temps = hourly.get('temperature_2m', [])
            h_precips = hourly.get('precipitation', [])

            for i, time_str in enumerate(h_times):
                if h_temps[i] is not None:
                    date_part, time_part = time_str.split('T')
                    hour_val = int(time_part.split(':')[0])
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

            logger.info(f"Extracted historical weather logs for {city}.")
            
        except Exception as e:
            logger.error(f"Failed to ingest historical data for {city}: {e}")

    async def ingest_forecast_data(self, city: str):
        """
        Get weather forecast for the next 7 days (used for live edge calculation)
        """
        city_upper = city.upper()
        if city_upper not in self.city_coords:
            return
            
        lat, lon = self.city_coords[city_upper]
        
        try:
            params = {
                "latitude": lat,
                "longitude": lon,
                "daily": ["temperature_2m_max", "precipitation_sum"],
                "hourly": ["temperature_2m", "precipitation"],
                "temperature_unit": "fahrenheit",
                "precipitation_unit": "inch",
                "timezone": "auto",
                "past_days": 1,
                "forecast_days": 7
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(self.base_url_forecast, params=params) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            
            # Log Daily Forecasts
            daily = data.get('daily', {})
            dates = daily.get('time', [])
            max_temps = daily.get('temperature_2m_max', [])
            precips = daily.get('precipitation_sum', [])
            
            for i, date_str in enumerate(dates):
                 if max_temps[i] is not None:
                      self.db.log_weather(
                          city=city_upper, target_date=date_str, 
                          max_temp_f=max_temps[i], precip_inch=precips[i] if precips[i] else 0, is_historical=False
                      )

            # Log Hourly Forecasts
            hourly = data.get('hourly', {})
            h_times = hourly.get('time', [])
            h_temps = hourly.get('temperature_2m', [])
            h_precips = hourly.get('precipitation', [])
            
            for i, time_str in enumerate(h_times):
                 if h_temps[i] is not None:
                      date_part, time_part = time_str.split('T')
                      hour_val = int(time_part.split(':')[0])
                      
                      self.db.log_weather(
                          city=city_upper, target_date=date_part, hour=hour_val,
                          max_temp_f=h_temps[i], precip_inch=h_precips[i] if h_precips[i] else 0, is_historical=False
                      )
            
            logger.info(f"🌤 Updated forecasts for {city}.")
            
        except Exception as e:
            logger.error(f"Weather API error for {city}: {e}")
