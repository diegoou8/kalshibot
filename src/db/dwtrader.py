import sqlite3
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from pathlib import Path

logger = logging.getLogger(__name__)

# Resolve the DB path relative to this package, so it is stable
# regardless of the current working directory.
_BASE_DIR = Path(__file__).resolve().parents[2]  # kalshi-bot-python/
_DEFAULT_DB_PATH = _BASE_DIR / "data" / "DWTrader.db"


class DWTraderDB:
    def __init__(self, db_path: str = str(_DEFAULT_DB_PATH)):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA busy_timeout = 10000')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        return conn

    def init_db(self):
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                
                # 1. SCANS
                c.execute('''
                    CREATE TABLE IF NOT EXISTS scans (
                        scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticker TEXT,
                        market_probability REAL,
                        ml_probability REAL,
                        best_bid INTEGER,
                        best_ask INTEGER,
                        spread INTEGER,
                        volume INTEGER,
                        timestamp DATETIME,
                        environment TEXT NOT NULL CHECK(environment IN ('SHADOW','PAPER','LIVE'))
                    )
                ''')

                # 2. DECISION LOG
                c.execute('''
                    CREATE TABLE IF NOT EXISTS decision_log (
                        decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scan_id INTEGER,
                        expected_value REAL,
                        kelly_fraction REAL,
                        risk_score REAL,
                        ml_probability REAL,
                        arbitrage_signal TEXT,
                        decision TEXT,
                        timestamp DATETIME,
                        environment TEXT NOT NULL CHECK(environment IN ('SHADOW','PAPER','LIVE')),
                        FOREIGN KEY(scan_id) REFERENCES scans(scan_id)
                    )
                ''')

                # 3. INTENTS
                c.execute('''
                    CREATE TABLE IF NOT EXISTS intents (
                        intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scan_id INTEGER,
                        ticker TEXT,
                        side TEXT,
                        expected_price_cents INTEGER,
                        target_qty INTEGER,
                        timestamp DATETIME,
                        status TEXT,
                        environment TEXT NOT NULL CHECK(environment IN ('SHADOW','PAPER','LIVE')),
                        FOREIGN KEY(scan_id) REFERENCES scans(scan_id)
                    )
                ''')

                # 4. ORDERS
                c.execute('''
                    CREATE TABLE IF NOT EXISTS orders (
                        order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        intent_id INTEGER,
                        exchange_order_id TEXT,
                        ticker TEXT,
                        side TEXT,
                        price_cents INTEGER,
                        qty INTEGER,
                        order_type TEXT,
                        status TEXT,
                        created_at DATETIME,
                        updated_at DATETIME,
                        environment TEXT NOT NULL CHECK(environment IN ('SHADOW','PAPER','LIVE')),
                        FOREIGN KEY(intent_id) REFERENCES intents(intent_id)
                    )
                ''')

                # 5. EXECUTIONS (Handles partial fills naturally since it links to order_id, scaling qty)
                c.execute('''
                    CREATE TABLE IF NOT EXISTS executions (
                        execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id INTEGER,
                        exchange_trade_id TEXT,
                        price_cents INTEGER,
                        qty INTEGER,
                        timestamp DATETIME,
                        environment TEXT NOT NULL CHECK(environment IN ('SHADOW','PAPER','LIVE')),
                        UNIQUE(order_id, exchange_trade_id),
                        FOREIGN KEY(order_id) REFERENCES orders(order_id)
                    )
                ''')

                # 6. POSITIONS
                c.execute('''
                    CREATE TABLE IF NOT EXISTS positions (
                        position_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticker TEXT,
                        side TEXT,
                        qty INTEGER,
                        avg_price_cents REAL,
                        cost_basis REAL,
                        realized_pnl_cents REAL,
                        unrealized_pnl_cents REAL,
                        updated_at DATETIME,
                        environment TEXT NOT NULL CHECK(environment IN ('SHADOW','PAPER','LIVE'))
                    )
                ''')

                # 7. POSITION EVENTS (Audit Log for Positions)
                c.execute('''
                    CREATE TABLE IF NOT EXISTS position_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        position_id INTEGER,
                        execution_id INTEGER,
                        event_type TEXT,
                        qty_change INTEGER,
                        price_cents INTEGER,
                        timestamp DATETIME,
                        environment TEXT NOT NULL CHECK(environment IN ('SHADOW','PAPER','LIVE')),
                        FOREIGN KEY(position_id) REFERENCES positions(position_id),
                        FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
                    )
                ''')

                # 8. WEATHER DATA (External Signals for ML & Rules)
                c.execute('''
                    CREATE TABLE IF NOT EXISTS weather_data (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        city TEXT NOT NULL,
                        target_date DATE NOT NULL,
                        hour INTEGER, -- Optional hour, NULL if it's daily data
                        max_temp_f REAL,
                        precip_inch REAL,
                        timestamp DATETIME,
                        is_historical BOOLEAN,
                        UNIQUE(city, target_date, hour)
                    )
                ''')

                # 9. PREDICTIONS (Brain output at trade time — for Brier tracking)
                c.execute('''
                    CREATE TABLE IF NOT EXISTS predictions (
                        prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticker TEXT NOT NULL,
                        trade_date DATE NOT NULL,
                        side TEXT NOT NULL,
                        predicted_p REAL NOT NULL,
                        actual_outcome INTEGER,
                        brier_score REAL,
                        city TEXT,
                        horizon_hrs REAL,
                        horizon_bin TEXT,
                        sigma REAL,
                        ar1_correction REAL,
                        recorded_at DATETIME NOT NULL
                    )
                ''')

                # 11. WEATHER ACTUALS — confirmed historical temps (separate from forecasts)
                c.execute('''
                    CREATE TABLE IF NOT EXISTS weather_actuals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        city TEXT NOT NULL,
                        target_date DATE NOT NULL,
                        hour INTEGER,
                        actual_temp_f REAL NOT NULL,
                        source TEXT DEFAULT 'open-meteo-archive',
                        recorded_at DATETIME NOT NULL,
                        UNIQUE(city, target_date, hour)
                    )
                ''')

                # 12. AR(1) RESIDUALS — per-city daily (forecast − actual) pairs for φ MLE
                c.execute('''
                    CREATE TABLE IF NOT EXISTS ar1_residuals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        city TEXT NOT NULL,
                        target_date DATE NOT NULL,
                        forecast_temp_f REAL NOT NULL,
                        actual_temp_f REAL NOT NULL,
                        error_f REAL NOT NULL,
                        recorded_at DATETIME NOT NULL,
                        UNIQUE(city, target_date)
                    )
                ''')

                # Migration: add calibration columns to predictions if they don't exist
                existing = {row[1] for row in c.execute("PRAGMA table_info(predictions)")}
                for col, defn in [
                    ("city",           "TEXT"),
                    ("horizon_hrs",    "REAL"),
                    ("horizon_bin",    "TEXT"),
                    ("sigma",          "REAL"),
                    ("ar1_correction", "REAL"),
                ]:
                    if col not in existing:
                        c.execute(f"ALTER TABLE predictions ADD COLUMN {col} {defn}")

                # 10. ORDERBOOK EVENTS (Raw microstructure feed)
                c.execute('''
                    CREATE TABLE IF NOT EXISTS orderbook_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticker TEXT,
                        msg_type TEXT,
                        payload TEXT,
                        timestamp DATETIME,
                        environment TEXT NOT NULL CHECK(environment IN ('SHADOW','PAPER','LIVE'))
                    )
                ''')

                conn.commit()
                logger.info("Database schema enriched with Explicit Foreign Keys and strict traceability.")
        except Exception as e:
            logger.error(f"Failed to initialize DWTrader database: {e}")

    # ==========================================
    # LOGGING THE LIFECYCLE (PROSPECT -> CLOSED)
    # ==========================================

    def log_scan(self, ticker: str, market_prob: float, ml_prob: float, 
                 best_bid: int, best_ask: int, spread: int, volume: int, environment: str) -> Optional[int]:
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute('''
                    INSERT INTO scans (
                        ticker, market_probability, ml_probability, best_bid, best_ask, spread, volume, timestamp, environment
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    ticker, market_prob, ml_prob, best_bid, best_ask, spread, volume, datetime.now().isoformat(), environment
                ))
                conn.commit()
                return c.lastrowid
        except Exception as e:
             logger.error(f"Error logging scan for {ticker}: {e}")
             return None

    def log_decision(self, scan_id: int, expected_value: float, kelly_fraction: float, 
                     risk_score: float, ml_prob: float, arb_signal: str, decision: str, environment: str) -> Optional[int]:
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute('''
                    INSERT INTO decision_log (
                        scan_id, expected_value, kelly_fraction, risk_score, ml_probability, arbitrage_signal, decision, timestamp, environment
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    scan_id, expected_value, kelly_fraction, risk_score, ml_prob, arb_signal, decision, datetime.now().isoformat(), environment
                ))
                conn.commit()
                return c.lastrowid
        except Exception as e:
             logger.error(f"Error logging decision for scan {scan_id}: {e}")
             return None

    def log_intent(self, scan_id: int, ticker: str, side: str, expected_price: int, 
                   target_qty: int, status: str, environment: str) -> Optional[int]:
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute('''
                    INSERT INTO intents (
                        scan_id, ticker, side, expected_price_cents, target_qty, timestamp, status, environment
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    scan_id, ticker, side, expected_price, target_qty, datetime.now().isoformat(), status, environment
                ))
                conn.commit()
                return c.lastrowid
        except Exception as e:
            logger.error(f"Error logging intent for {ticker}: {e}")
            return None

    def log_order(self, intent_id: Optional[int], exchange_order_id: str, ticker: str, side: str,
                  price: int, qty: int, order_type: str, status: str, environment: str) -> Optional[int]:
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                now = datetime.now().isoformat()
                c.execute('''
                    INSERT INTO orders (
                        intent_id, exchange_order_id, ticker, side, price_cents, qty, order_type,
                        status, created_at, updated_at, environment
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    intent_id, exchange_order_id, ticker, side, price, qty, order_type,
                    status, now, now, environment
                ))
                conn.commit()
                return c.lastrowid
        except Exception as e:
             logger.error(f"Error logging order: {e}")
             return None

    def log_execution(self, order_id: int, exchange_trade_id: str, ticker: str, 
                      side: str, price: int, qty: int, environment: str) -> Optional[int]:
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                now = datetime.now().isoformat()
                
                # 1. Log Execution
                c.execute('''
                    INSERT INTO executions (
                        order_id, exchange_trade_id, price_cents, qty, timestamp, environment
                    ) VALUES (?, ?, ?, ?, ?, ?)
                ''', (order_id, exchange_trade_id, price, qty, now, environment))
                execution_id = c.lastrowid
                
                # 2. Upsert Position (Tracking Cost Basis)
                c.execute('''SELECT position_id, qty, avg_price_cents, cost_basis FROM positions 
                             WHERE ticker = ? AND side = ? AND environment = ?''', (ticker, side, environment))
                pos = c.fetchone()
                
                if pos:
                     old_qty = pos['qty']
                     new_qty = old_qty + qty
                     new_avg = ((old_qty * pos['avg_price_cents']) + (qty * price)) / new_qty
                     new_cost_basis = pos['cost_basis'] + ((price / 100.0) * qty)
                     
                     c.execute('''
                         UPDATE positions SET qty = ?, avg_price_cents = ?, cost_basis = ?, updated_at = ?
                         WHERE position_id = ?
                     ''', (new_qty, new_avg, new_cost_basis, now, pos['position_id']))
                     pos_id = pos['position_id']
                else:
                     cost_basis = (price / 100.0) * qty
                     c.execute('''
                         INSERT INTO positions (
                             ticker, side, qty, avg_price_cents, cost_basis, realized_pnl_cents, unrealized_pnl_cents, updated_at, environment
                         ) VALUES (?, ?, ?, ?, ?, 0.0, 0.0, ?, ?)
                     ''', (ticker, side, qty, price, cost_basis, now, environment))
                     pos_id = c.lastrowid
                     
                # 3. Log Position Event
                if pos_id:
                     c.execute('''
                         INSERT INTO position_events (
                             position_id, execution_id, event_type, qty_change, price_cents, timestamp, environment
                         ) VALUES (?, ?, 'INCREASE', ?, ?, ?, ?)
                     ''', (pos_id, execution_id, qty, price, now, environment))
                     
                conn.commit()
                return execution_id
                
        except sqlite3.IntegrityError:
             logger.warning(f"Duplicate execution ignored for trade_id: {exchange_trade_id}")
             return None
        except Exception as e:
             logger.error(f"Error logging execution: {e}")
             return None

    # ==========================================
    # LOGGING EXTERNAL SIGNALS (WEATHER)
    # ==========================================
    
    def log_weather(self, city: str, target_date: str, max_temp_f: float, precip_inch: float, 
                    is_historical: bool, hour: Optional[int] = None) -> Optional[int]:
        """Logs weather data for future ML inference. Unique by city, date, and hour."""
        try:
             with self.get_connection() as conn:
                 c = conn.cursor()
                 now = datetime.now().isoformat()
                 c.execute('''
                     INSERT OR REPLACE INTO weather_data (
                         city, target_date, hour, max_temp_f, precip_inch, timestamp, is_historical
                     ) VALUES (?, ?, ?, ?, ?, ?, ?)
                 ''', (city, target_date, hour, max_temp_f, precip_inch, now, is_historical))
                 conn.commit()
                 return c.lastrowid
        except Exception as e:
             logger.error(f"Error logging weather data for {city} on {target_date}: {e}")
             return None

    def log_prediction(self, ticker: str, trade_date: str, side: str,
                       predicted_p: float, city: Optional[str] = None,
                       horizon_hrs: Optional[float] = None,
                       horizon_bin: Optional[str] = None,
                       sigma: Optional[float] = None,
                       ar1_correction: Optional[float] = None) -> Optional[int]:
        """Record brain's predicted P(YES) at trade time for later Brier scoring."""
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    '''
                    INSERT INTO predictions
                        (ticker, trade_date, side, predicted_p, city, horizon_hrs,
                         horizon_bin, sigma, ar1_correction, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (ticker, trade_date, side, predicted_p, city, horizon_hrs,
                     horizon_bin, sigma, ar1_correction, datetime.now().isoformat()),
                )
                conn.commit()
                return c.lastrowid
        except Exception as e:
            logger.error(f"Error logging prediction for {ticker}: {e}")
            return None

    def update_prediction_outcome(self, ticker: str, trade_date: str,
                                   actual_outcome: int) -> int:
        """
        Write settlement result back into predictions and compute Brier score.
        actual_outcome: 1 = YES settled, 0 = NO settled.
        Returns number of rows updated.
        """
        try:
            with self.get_connection() as conn:
                rows = conn.execute(
                    "SELECT prediction_id, predicted_p FROM predictions "
                    "WHERE ticker = ? AND trade_date = ? AND actual_outcome IS NULL",
                    (ticker, trade_date),
                ).fetchall()
                updated = 0
                for row in rows:
                    brier = (row["predicted_p"] - actual_outcome) ** 2
                    conn.execute(
                        "UPDATE predictions SET actual_outcome = ?, brier_score = ? "
                        "WHERE prediction_id = ?",
                        (actual_outcome, brier, row["prediction_id"]),
                    )
                    updated += 1
                conn.commit()
                return updated
        except Exception as e:
            logger.error(f"Error updating prediction outcome for {ticker}: {e}")
            return 0

    def get_brier_summary(self, city: Optional[str] = None,
                          horizon_bin: Optional[str] = None) -> dict:
        """Aggregate Brier score stats for calibration monitoring."""
        try:
            with self.get_connection() as conn:
                where, params = ["actual_outcome IS NOT NULL"], []
                if city:
                    where.append("city = ?"); params.append(city)
                if horizon_bin:
                    where.append("horizon_bin = ?"); params.append(horizon_bin)
                clause = " AND ".join(where)
                row = conn.execute(
                    f"SELECT COUNT(*) n, AVG(brier_score) avg_brier, "
                    f"MIN(brier_score) min_brier, MAX(brier_score) max_brier "
                    f"FROM predictions WHERE {clause}",
                    params,
                ).fetchone()
                return dict(row) if row else {}
        except Exception as e:
            logger.error(f"Error fetching Brier summary: {e}")
            return {}

    def log_weather_actual(self, city: str, target_date: str,
                           actual_temp_f: float, hour: Optional[int] = None) -> Optional[int]:
        """Store confirmed actual temperature from archive API (separate from forecasts)."""
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    '''
                    INSERT OR REPLACE INTO weather_actuals
                        (city, target_date, hour, actual_temp_f, recorded_at)
                    VALUES (?, ?, ?, ?, ?)
                    ''',
                    (city, target_date, hour, actual_temp_f, datetime.now().isoformat()),
                )
                conn.commit()
                return c.lastrowid
        except Exception as e:
            logger.error(f"Error logging weather actual for {city} {target_date}: {e}")
            return None

    def log_ar1_residual(self, city: str, target_date: str,
                         forecast_temp_f: float, actual_temp_f: float) -> Optional[int]:
        """Persist (forecast, actual, error) pair for AR(1) φ estimation."""
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    '''
                    INSERT OR REPLACE INTO ar1_residuals
                        (city, target_date, forecast_temp_f, actual_temp_f, error_f, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ''',
                    (city, target_date, forecast_temp_f, actual_temp_f,
                     actual_temp_f - forecast_temp_f, datetime.now().isoformat()),
                )
                conn.commit()
                return c.lastrowid
        except Exception as e:
            logger.error(f"Error logging AR(1) residual for {city} {target_date}: {e}")
            return None

    def get_ar1_phi_estimate(self, city: str, min_days: int = 14) -> Optional[float]:
        """
        Estimate AR(1) coefficient φ from stored residuals using OLS:
          error_t = φ × error_{t-1} + ε
        Returns None if fewer than min_days data points available.
        """
        try:
            with self.get_connection() as conn:
                rows = conn.execute(
                    "SELECT error_f FROM ar1_residuals WHERE city = ? "
                    "ORDER BY target_date ASC",
                    (city,),
                ).fetchall()
            errors = [r["error_f"] for r in rows]
            if len(errors) < min_days:
                return None
            # OLS: φ = Σ(e_t × e_{t-1}) / Σ(e_{t-1}²)
            pairs = list(zip(errors[1:], errors[:-1]))
            num = sum(e_t * e_tm1 for e_t, e_tm1 in pairs)
            den = sum(e_tm1 ** 2 for _, e_tm1 in pairs)
            return num / den if den > 0 else None
        except Exception as e:
            logger.error(f"Error estimating AR(1) phi for {city}: {e}")
            return None

    def get_sigma_mle(self, city: str, min_days: int = 14) -> Optional[float]:
        """
        MLE estimate of forecast error σ (°F) from stored AR(1) residuals:
          σ = sqrt((1/N) Σ eᵢ²)
        Returns None if fewer than min_days data points available.
        """
        try:
            with self.get_connection() as conn:
                rows = conn.execute(
                    "SELECT error_f FROM ar1_residuals WHERE city = ? "
                    "ORDER BY target_date ASC",
                    (city,),
                ).fetchall()
            errors = [r["error_f"] for r in rows]
            if len(errors) < min_days:
                return None
            import math
            return math.sqrt(sum(e ** 2 for e in errors) / len(errors))
        except Exception as e:
            logger.error(f"Error computing sigma MLE for {city}: {e}")
            return None

    def get_daily_realized_pnl(self, environment: str, trade_date: Optional[str] = None) -> float:
        """
        Sum realized P&L (cents) for all executions on trade_date.
        Uses: payout = (100 - fill_price) * qty  for wins, -fill_price * qty for losses.
        Since we can't know settlement here, returns raw cost basis as a loss proxy:
        returns the total dollars spent today (negative = spent money).
        """
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")
        try:
            with self.get_connection() as conn:
                row = conn.execute(
                    '''
                    SELECT COALESCE(SUM(e.price_cents * e.qty), 0) AS total_spent_cents
                    FROM executions e
                    WHERE DATE(e.timestamp) = ? AND e.environment = ?
                    ''',
                    (trade_date, environment),
                ).fetchone()
                return float(row[0]) / 100.0 if row else 0.0
        except Exception as e:
            logger.error(f"Error fetching daily P&L: {e}")
            return 0.0

    def log_orderbook_event(self, ticker: str, msg_type: str, payload: str, environment: str) -> Optional[int]:
        """
        Persist raw orderbook snapshots/deltas as JSON payloads so they can be
        replayed in microstructure analysis and toxicity filters later.
        """
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                now = datetime.now().isoformat()
                c.execute(
                    '''
                    INSERT INTO orderbook_events (ticker, msg_type, payload, timestamp, environment)
                    VALUES (?, ?, ?, ?, ?)
                    ''',
                    (ticker, msg_type, payload, now, environment),
                )
                conn.commit()
                return c.lastrowid
        except Exception as e:
            logger.error(f"Error logging orderbook event for {ticker}: {e}")
            return None

    def log_execution_record(
        self,
        order_id: int,
        exchange_trade_id: str,
        price_cents: int,
        qty: int,
        environment: str,
    ) -> Optional[int]:
        """
        Insert a raw fill into the executions table without touching positions.
        Used for sell fills — position accounting is handled separately by
        log_position_close so the positions table is not incremented.
        """
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    """
                    INSERT INTO executions
                        (order_id, exchange_trade_id, price_cents, qty, timestamp, environment)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (order_id, exchange_trade_id, price_cents, qty,
                     datetime.now().isoformat(), environment),
                )
                conn.commit()
                return c.lastrowid
        except sqlite3.IntegrityError:
            logger.warning("Duplicate sell execution ignored: %s", exchange_trade_id)
            return None
        except Exception as e:
            logger.error("Error logging sell execution record: %s", e)
            return None

    def get_open_positions(self, environment: str) -> List[Dict[str, Any]]:
        """Return all positions with qty > 0 for the given environment."""
        try:
            with self.get_connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM positions WHERE qty > 0 AND environment = ?",
                    (environment.upper(),),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"Error fetching open positions: {e}")
            return []

    def update_position_pnl(self, position_id: int, unrealized_pnl_cents: float) -> None:
        """Refresh unrealized_pnl_cents for a position from live market data."""
        try:
            with self.get_connection() as conn:
                conn.execute(
                    "UPDATE positions SET unrealized_pnl_cents = ?, updated_at = ? WHERE position_id = ?",
                    (unrealized_pnl_cents, datetime.now().isoformat(), position_id),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Error updating PnL for position {position_id}: {e}")

    def log_position_close(
        self,
        position_id: int,
        fill_price_cents: int,
        qty_sold: int,
        realized_pnl_cents: float,
        exit_reason: str = "",
    ) -> None:
        """
        Record a position reduction: decrease qty, credit realized PnL, log DECREASE event.
        Called when a sell order fills — NOT called for settlement (that goes through
        update_prediction_outcome / check_outcomes.py).
        """
        try:
            with self.get_connection() as conn:
                now = datetime.now().isoformat()
                conn.execute(
                    """
                    UPDATE positions
                    SET qty                 = MAX(0, qty - ?),
                        realized_pnl_cents  = realized_pnl_cents + ?,
                        unrealized_pnl_cents = 0.0,
                        updated_at          = ?
                    WHERE position_id = ?
                    """,
                    (qty_sold, realized_pnl_cents, now, position_id),
                )
                event_type = f"DECREASE:{exit_reason}" if exit_reason else "DECREASE"
                conn.execute(
                    """
                    INSERT INTO position_events
                        (position_id, execution_id, event_type, qty_change, price_cents, timestamp, environment)
                    SELECT ?, NULL, ?, ?, ?, ?, environment
                    FROM positions WHERE position_id = ?
                    """,
                    (position_id, event_type, -qty_sold, fill_price_cents, now, position_id),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Error closing position {position_id}: {e}")
