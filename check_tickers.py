import sqlite3
import pandas as pd
from src.db.dwtrader import _DEFAULT_DB_PATH

conn = sqlite3.connect(_DEFAULT_DB_PATH)
scans = pd.read_sql("SELECT DISTINCT ticker FROM scans LIMIT 50", conn)
print(scans)
