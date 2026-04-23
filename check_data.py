import sqlite3
import pandas as pd
from src.db.dwtrader import _DEFAULT_DB_PATH

conn = sqlite3.connect(_DEFAULT_DB_PATH)

scans = pd.read_sql("SELECT ticker, count(*) as count, min(timestamp) as min_ts, max(timestamp) as max_ts FROM scans GROUP BY ticker ORDER BY count DESC LIMIT 10", conn)
weather = pd.read_sql("SELECT city, target_date, count(*) as count, min(timestamp) as min_ts, max(timestamp) as max_ts FROM weather_data GROUP BY city, target_date ORDER BY count DESC LIMIT 10", conn)

print("Scans summary:")
print(scans)
print("\nWeather summary:")
print(weather)
