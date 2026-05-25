import os
import logging
import pyodbc
import math
import re as _re_m
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger(__name__)


class DWTraderDB:
    def __init__(self, conn_str: Optional[str] = None):
        self.conn_str = conn_str or os.environ.get("AZURE_SQL_CONN_STR")
        if not self.conn_str:
            raise RuntimeError(
                "AZURE_SQL_CONN_STR is not set. "
                "Add it to .env locally or as an Azure Web App application setting."
            )
        self.init_db()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def get_connection(self) -> pyodbc.Connection:
        return pyodbc.connect(self.conn_str, autocommit=False)

    # ------------------------------------------------------------------
    # Row helper — converts a pyodbc Row to a plain dict
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(cursor: pyodbc.Cursor, row) -> dict:
        return {desc[0]: val for desc, val in zip(cursor.description, row)}

    @staticmethod
    def _rows_to_dicts(cursor: pyodbc.Cursor, rows: list) -> List[dict]:
        return [{desc[0]: val for desc, val in zip(cursor.description, row)} for row in rows]

    # ------------------------------------------------------------------
    # Last inserted identity
    # ------------------------------------------------------------------

    @staticmethod
    def _last_id(cursor: pyodbc.Cursor) -> Optional[int]:
        cursor.execute("SELECT SCOPE_IDENTITY()")
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    # ------------------------------------------------------------------
    # Schema migration helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _create_table_if_not_exists(cursor: pyodbc.Cursor, table: str, ddl: str) -> None:
        cursor.execute(
            f"IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
            f"WHERE TABLE_NAME = '{table}') BEGIN {ddl} END"
        )

    @staticmethod
    def _add_column_if_not_exists(
        cursor: pyodbc.Cursor, table: str, column: str, definition: str
    ) -> None:
        cursor.execute(
            f"IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
            f"WHERE TABLE_NAME = '{table}' AND COLUMN_NAME = '{column}') "
            f"ALTER TABLE {table} ADD {column} {definition}"
        )

    # ------------------------------------------------------------------
    # init_db — create all tables + run inline migrations
    # ------------------------------------------------------------------

    def init_db(self) -> None:
        try:
            with self.get_connection() as conn:
                c = conn.cursor()

                # 1. SCANS
                self._create_table_if_not_exists(c, "scans", """
                    CREATE TABLE scans (
                        scan_id      INT IDENTITY(1,1) PRIMARY KEY,
                        ticker       NVARCHAR(MAX),
                        market_probability FLOAT,
                        ml_probability     FLOAT,
                        best_bid     INT,
                        best_ask     INT,
                        spread       INT,
                        volume       INT,
                        timestamp    DATETIME2,
                        environment  NVARCHAR(10) NOT NULL
                            CHECK(environment IN ('SHADOW','PAPER','LIVE'))
                    )
                """)

                # 2. DECISION LOG
                self._create_table_if_not_exists(c, "decision_log", """
                    CREATE TABLE decision_log (
                        decision_id      INT IDENTITY(1,1) PRIMARY KEY,
                        scan_id          INT,
                        expected_value   FLOAT,
                        kelly_fraction   FLOAT,
                        risk_score       FLOAT,
                        ml_probability   FLOAT,
                        arbitrage_signal NVARCHAR(MAX),
                        decision         NVARCHAR(MAX),
                        timestamp        DATETIME2,
                        environment      NVARCHAR(10) NOT NULL
                            CHECK(environment IN ('SHADOW','PAPER','LIVE')),
                        FOREIGN KEY(scan_id) REFERENCES scans(scan_id)
                    )
                """)

                # 3. INTENTS
                self._create_table_if_not_exists(c, "intents", """
                    CREATE TABLE intents (
                        intent_id           INT IDENTITY(1,1) PRIMARY KEY,
                        scan_id             INT,
                        ticker              NVARCHAR(MAX),
                        side                NVARCHAR(MAX),
                        expected_price_cents INT,
                        target_qty          INT,
                        timestamp           DATETIME2,
                        status              NVARCHAR(MAX),
                        environment         NVARCHAR(10) NOT NULL
                            CHECK(environment IN ('SHADOW','PAPER','LIVE')),
                        FOREIGN KEY(scan_id) REFERENCES scans(scan_id)
                    )
                """)

                # 4. ORDERS
                self._create_table_if_not_exists(c, "orders", """
                    CREATE TABLE orders (
                        order_id          INT IDENTITY(1,1) PRIMARY KEY,
                        intent_id         INT,
                        exchange_order_id NVARCHAR(MAX),
                        ticker            NVARCHAR(MAX),
                        side              NVARCHAR(MAX),
                        price_cents       INT,
                        qty               INT,
                        order_type        NVARCHAR(MAX),
                        status            NVARCHAR(MAX),
                        created_at        DATETIME2,
                        updated_at        DATETIME2,
                        environment       NVARCHAR(10) NOT NULL
                            CHECK(environment IN ('SHADOW','PAPER','LIVE')),
                        gumbel_mode       NVARCHAR(MAX),
                        FOREIGN KEY(intent_id) REFERENCES intents(intent_id)
                    )
                """)

                # 5. EXECUTIONS
                self._create_table_if_not_exists(c, "executions", """
                    CREATE TABLE executions (
                        execution_id     INT IDENTITY(1,1) PRIMARY KEY,
                        order_id         INT,
                        exchange_trade_id NVARCHAR(100),
                        price_cents      INT,
                        qty              INT,
                        timestamp        DATETIME2,
                        environment      NVARCHAR(10) NOT NULL
                            CHECK(environment IN ('SHADOW','PAPER','LIVE')),
                        lvr_cents        FLOAT,
                        CONSTRAINT uq_executions_order_trade
                            UNIQUE(order_id, exchange_trade_id),
                        FOREIGN KEY(order_id) REFERENCES orders(order_id)
                    )
                """)

                # 6. POSITIONS
                self._create_table_if_not_exists(c, "positions", """
                    CREATE TABLE positions (
                        position_id         INT IDENTITY(1,1) PRIMARY KEY,
                        ticker              NVARCHAR(MAX),
                        side                NVARCHAR(MAX),
                        qty                 INT,
                        avg_price_cents     FLOAT,
                        cost_basis          FLOAT,
                        realized_pnl_cents  FLOAT,
                        unrealized_pnl_cents FLOAT,
                        updated_at          DATETIME2,
                        status              NVARCHAR(50) DEFAULT 'open',
                        environment         NVARCHAR(10) NOT NULL
                            CHECK(environment IN ('SHADOW','PAPER','LIVE')),
                        gumbel_mode         NVARCHAR(MAX)
                    )
                """)

                # Migration: status column (already in DDL above; guard for pre-existing tables)
                self._add_column_if_not_exists(c, "positions", "status", "NVARCHAR(50) DEFAULT 'open'")
                conn.commit()

                # One-time: mark positions whose ticker date is before today as settled
                today_iso = datetime.now().strftime("%Y-%m-%d")
                c.execute(
                    "SELECT position_id, ticker FROM positions WHERE status IS NULL OR status = 'open'"
                )
                pos_rows = c.fetchall()
                for pos_row in pos_rows:
                    pos_id, tkr = pos_row[0], pos_row[1]
                    m = _re_m.match(
                        r"KX(?:HIGH|TEMP)[A-Z]+-(\d{2}[A-Z]{3}\d{2})",
                        tkr or "",
                        _re_m.IGNORECASE,
                    )
                    if m:
                        try:
                            tkr_date = datetime.strptime(
                                m.group(1).upper(), "%y%b%d"
                            ).strftime("%Y-%m-%d")
                            if tkr_date < today_iso:
                                c.execute(
                                    "UPDATE positions SET status='settled' WHERE position_id=?",
                                    (pos_id,),
                                )
                        except Exception:
                            pass
                conn.commit()

                # Migration: gumbel_mode columns
                self._add_column_if_not_exists(c, "orders",    "gumbel_mode", "NVARCHAR(MAX)")
                self._add_column_if_not_exists(c, "positions", "gumbel_mode", "NVARCHAR(MAX)")
                conn.commit()

                # Backfill gumbel_mode on orders from the A/B/C experiment schedule
                _GM_SCHEDULE = {
                    "2026-04-28": "half",
                    "2026-04-29": "none",
                    "2026-04-30": "full",
                }
                for _date, _mode in _GM_SCHEDULE.items():
                    c.execute(
                        "UPDATE orders SET gumbel_mode = ? "
                        "WHERE CAST(created_at AS DATE) = ? AND gumbel_mode IS NULL",
                        (_mode, _date),
                    )
                conn.commit()

                # Backfill gumbel_mode on positions from their earliest matching order
                c.execute("SELECT position_id FROM positions WHERE gumbel_mode IS NULL")
                null_pos_rows = c.fetchall()
                for (null_pos_id,) in null_pos_rows:
                    c.execute(
                        """
                        SELECT TOP 1 o.gumbel_mode FROM orders o
                        JOIN executions e ON e.order_id = o.order_id
                        JOIN positions p ON p.ticker = o.ticker
                        WHERE p.position_id = ? AND o.gumbel_mode IS NOT NULL
                        ORDER BY o.created_at ASC
                        """,
                        (null_pos_id,),
                    )
                    gm_row = c.fetchone()
                    if gm_row and gm_row[0]:
                        c.execute(
                            "UPDATE positions SET gumbel_mode = ? WHERE position_id = ?",
                            (gm_row[0], null_pos_id),
                        )
                conn.commit()

                # 7. POSITION EVENTS
                self._create_table_if_not_exists(c, "position_events", """
                    CREATE TABLE position_events (
                        event_id     INT IDENTITY(1,1) PRIMARY KEY,
                        position_id  INT,
                        execution_id INT,
                        event_type   NVARCHAR(MAX),
                        qty_change   INT,
                        price_cents  INT,
                        timestamp    DATETIME2,
                        environment  NVARCHAR(10) NOT NULL
                            CHECK(environment IN ('SHADOW','PAPER','LIVE')),
                        FOREIGN KEY(position_id)  REFERENCES positions(position_id),
                        FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
                    )
                """)

                # 8. WEATHER DATA
                self._create_table_if_not_exists(c, "weather_data", """
                    CREATE TABLE weather_data (
                        id            INT IDENTITY(1,1) PRIMARY KEY,
                        city          NVARCHAR(100) NOT NULL,
                        target_date   DATE NOT NULL,
                        hour          INT,
                        max_temp_f    FLOAT,
                        precip_inch   FLOAT,
                        timestamp     DATETIME2,
                        is_historical BIT,
                        CONSTRAINT uq_weather_data_city_date_hour
                            UNIQUE(city, target_date, hour)
                    )
                """)

                # 9. PREDICTIONS
                self._create_table_if_not_exists(c, "predictions", """
                    CREATE TABLE predictions (
                        prediction_id  INT IDENTITY(1,1) PRIMARY KEY,
                        ticker         NVARCHAR(MAX) NOT NULL,
                        trade_date     DATE NOT NULL,
                        side           NVARCHAR(MAX) NOT NULL,
                        predicted_p    FLOAT NOT NULL,
                        actual_outcome INT,
                        brier_score    FLOAT,
                        city           NVARCHAR(MAX),
                        horizon_hrs    FLOAT,
                        horizon_bin    NVARCHAR(MAX),
                        sigma          FLOAT,
                        ar1_correction FLOAT,
                        recorded_at    DATETIME2 NOT NULL
                    )
                """)

                # 11. WEATHER ACTUALS
                self._create_table_if_not_exists(c, "weather_actuals", """
                    CREATE TABLE weather_actuals (
                        id            INT IDENTITY(1,1) PRIMARY KEY,
                        city          NVARCHAR(100) NOT NULL,
                        target_date   DATE NOT NULL,
                        hour          INT,
                        actual_temp_f FLOAT NOT NULL,
                        source        NVARCHAR(MAX) DEFAULT 'open-meteo-archive',
                        recorded_at   DATETIME2 NOT NULL,
                        CONSTRAINT uq_weather_actuals_city_date_hour
                            UNIQUE(city, target_date, hour)
                    )
                """)

                # 12. AR(1) RESIDUALS
                self._create_table_if_not_exists(c, "ar1_residuals", """
                    CREATE TABLE ar1_residuals (
                        id             INT IDENTITY(1,1) PRIMARY KEY,
                        city           NVARCHAR(100) NOT NULL,
                        target_date    DATE NOT NULL,
                        forecast_temp_f FLOAT NOT NULL,
                        actual_temp_f  FLOAT NOT NULL,
                        error_f        FLOAT NOT NULL,
                        recorded_at    DATETIME2 NOT NULL,
                        horizon_hrs    FLOAT,
                        CONSTRAINT uq_ar1_residuals_city_date
                            UNIQUE(city, target_date)
                    )
                """)
                conn.commit()

                # Migration: calibration columns on predictions
                for col, defn in [
                    ("city",           "NVARCHAR(MAX)"),
                    ("horizon_hrs",    "FLOAT"),
                    ("horizon_bin",    "NVARCHAR(MAX)"),
                    ("sigma",          "FLOAT"),
                    ("ar1_correction", "FLOAT"),
                ]:
                    self._add_column_if_not_exists(c, "predictions", col, defn)

                # Migration: horizon_hrs on ar1_residuals
                self._add_column_if_not_exists(c, "ar1_residuals", "horizon_hrs", "FLOAT")
                conn.commit()

                # 10. ORDERBOOK EVENTS
                self._create_table_if_not_exists(c, "orderbook_events", """
                    CREATE TABLE orderbook_events (
                        id          INT IDENTITY(1,1) PRIMARY KEY,
                        ticker      NVARCHAR(MAX),
                        msg_type    NVARCHAR(MAX),
                        payload     NVARCHAR(MAX),
                        timestamp   DATETIME2,
                        environment NVARCHAR(10) NOT NULL
                            CHECK(environment IN ('SHADOW','PAPER','LIVE'))
                    )
                """)

                # 13. TRADE ATTRIBUTION
                self._create_table_if_not_exists(c, "trade_attribution", """
                    CREATE TABLE trade_attribution (
                        attribution_id       INT IDENTITY(1,1) PRIMARY KEY,
                        execution_id         INT REFERENCES executions(execution_id),
                        ticker               NVARCHAR(100) NOT NULL,
                        city                 NVARCHAR(100),
                        side                 NVARCHAR(10),
                        horizon_bin          NVARCHAR(50),
                        fill_price_cents     INT,
                        mid_at_fill_cents    INT,
                        predicted_p          FLOAT,
                        market_implied_p     FLOAT,
                        realized_outcome     INT,
                        expected_value_cents FLOAT,
                        realized_pnl_cents   FLOAT,
                        slippage_cents       FLOAT,
                        fees_cents           FLOAT,
                        holding_time_hrs     FLOAT,
                        recorded_at          DATETIME2 NOT NULL
                    )
                """)
                c.execute(
                    "IF NOT EXISTS (SELECT 1 FROM sys.indexes "
                    "WHERE name='idx_trade_attribution_ticker_ts') "
                    "CREATE INDEX idx_trade_attribution_ticker_ts "
                    "ON trade_attribution (ticker, recorded_at)"
                )

                # 14. CALIBRATION DIAGNOSTICS
                self._create_table_if_not_exists(c, "calibration_diagnostics", """
                    CREATE TABLE calibration_diagnostics (
                        id                     INT IDENTITY(1,1) PRIMARY KEY,
                        ts                     NVARCHAR(50) NOT NULL,
                        ticker                 NVARCHAR(100) NOT NULL,
                        city                   NVARCHAR(100),
                        horizon_bucket         NVARCHAR(50),
                        strike_distance_bucket NVARCHAR(50),
                        p_model                FLOAT,
                        p_market               FLOAT,
                        edge                   FLOAT,
                        trade_side             NVARCHAR(10),
                        gumbel_mode            NVARCHAR(20),
                        env_mode               NVARCHAR(10)
                    )
                """)
                c.execute(
                    "IF NOT EXISTS (SELECT 1 FROM sys.indexes "
                    "WHERE name='idx_calib_diag_city_ts') "
                    "CREATE INDEX idx_calib_diag_city_ts "
                    "ON calibration_diagnostics (city, ts)"
                )

                # 15. EXPERIMENT RUNS
                self._create_table_if_not_exists(c, "experiment_runs", """
                    CREATE TABLE experiment_runs (
                        id                  INT IDENTITY(1,1) PRIMARY KEY,
                        run_date            NVARCHAR(20) NOT NULL,
                        gumbel_mode         NVARCHAR(20),
                        total_trades        INT DEFAULT 0,
                        yes_trades          INT DEFAULT 0,
                        no_trades           INT DEFAULT 0,
                        avg_edge_cents      FLOAT,
                        avg_lvr_cents       FLOAT,
                        realized_pnl_cents  FLOAT,
                        brier_score         FLOAT,
                        n_settled           INT DEFAULT 0,
                        recorded_at         NVARCHAR(MAX) NOT NULL
                    )
                """)
                c.execute(
                    "IF NOT EXISTS (SELECT 1 FROM sys.indexes "
                    "WHERE name='uq_experiment_runs_date_mode') "
                    "CREATE UNIQUE INDEX uq_experiment_runs_date_mode "
                    "ON experiment_runs (run_date, gumbel_mode)"
                )

                # Migration: lvr_cents on executions
                self._add_column_if_not_exists(c, "executions", "lvr_cents", "FLOAT")

                # 16. BOT CONFIG — runtime key/value overrides (e.g. GUMBEL_MODE)
                # Note: 'key' is reserved in SQL Server — column is named config_key.
                self._create_table_if_not_exists(c, "bot_config", """
                    CREATE TABLE bot_config (
                        config_key NVARCHAR(100) PRIMARY KEY,
                        value      NVARCHAR(MAX) NOT NULL,
                        updated_at DATETIME2     NOT NULL
                    )
                """)

                conn.commit()
                logger.info("Azure SQL schema initialised / migrations applied.")
        except Exception as e:
            logger.error(f"Failed to initialize DWTrader database: {e}")

    # ==========================================
    # LOGGING THE LIFECYCLE (PROSPECT -> CLOSED)
    # ==========================================

    def log_scan(
        self,
        ticker: str,
        market_prob: float,
        ml_prob: float,
        best_bid: int,
        best_ask: int,
        spread: int,
        volume: int,
        environment: str,
    ) -> Optional[int]:
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    """
                    INSERT INTO scans (
                        ticker, market_probability, ml_probability,
                        best_bid, best_ask, spread, volume, timestamp, environment
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticker, market_prob, ml_prob,
                        best_bid, best_ask, spread, volume,
                        datetime.now().isoformat(), environment,
                    ),
                )
                row_id = self._last_id(c)
                conn.commit()
                return row_id
        except Exception as e:
            logger.error(f"Error logging scan for {ticker}: {e}")
            return None

    def log_decision(
        self,
        scan_id: int,
        expected_value: float,
        kelly_fraction: float,
        risk_score: float,
        ml_prob: float,
        arb_signal: str,
        decision: str,
        environment: str,
    ) -> Optional[int]:
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    """
                    INSERT INTO decision_log (
                        scan_id, expected_value, kelly_fraction, risk_score,
                        ml_probability, arbitrage_signal, decision, timestamp, environment
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan_id, expected_value, kelly_fraction, risk_score,
                        ml_prob, arb_signal, decision,
                        datetime.now().isoformat(), environment,
                    ),
                )
                row_id = self._last_id(c)
                conn.commit()
                return row_id
        except Exception as e:
            logger.error(f"Error logging decision for scan {scan_id}: {e}")
            return None

    def log_intent(
        self,
        scan_id: int,
        ticker: str,
        side: str,
        expected_price: int,
        target_qty: int,
        status: str,
        environment: str,
    ) -> Optional[int]:
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    """
                    INSERT INTO intents (
                        scan_id, ticker, side, expected_price_cents,
                        target_qty, timestamp, status, environment
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan_id, ticker, side, expected_price,
                        target_qty, datetime.now().isoformat(), status, environment,
                    ),
                )
                row_id = self._last_id(c)
                conn.commit()
                return row_id
        except Exception as e:
            logger.error(f"Error logging intent for {ticker}: {e}")
            return None

    def log_order(
        self,
        intent_id: Optional[int],
        exchange_order_id: str,
        ticker: str,
        side: str,
        price: int,
        qty: int,
        order_type: str,
        status: str,
        environment: str,
        gumbel_mode: Optional[str] = None,
    ) -> Optional[int]:
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                now = datetime.now().isoformat()
                c.execute(
                    """
                    INSERT INTO orders (
                        intent_id, exchange_order_id, ticker, side, price_cents, qty,
                        order_type, status, created_at, updated_at, environment, gumbel_mode
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent_id, exchange_order_id, ticker, side, price, qty,
                        order_type, status, now, now, environment, gumbel_mode,
                    ),
                )
                row_id = self._last_id(c)
                conn.commit()
                return row_id
        except Exception as e:
            logger.error(f"Error logging order: {e}")
            return None

    def log_execution(
        self,
        order_id: int,
        exchange_trade_id: str,
        ticker: str,
        side: str,
        price: int,
        qty: int,
        environment: str,
        lvr_cents: Optional[float] = None,
        gumbel_mode: Optional[str] = None,
    ) -> Optional[int]:
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                now = datetime.now().isoformat()

                # 1. Log Execution
                c.execute(
                    """
                    INSERT INTO executions (
                        order_id, exchange_trade_id, price_cents, qty,
                        timestamp, environment, lvr_cents
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (order_id, exchange_trade_id, price, qty, now, environment, lvr_cents),
                )
                execution_id = self._last_id(c)

                # 2. Upsert Position (Tracking Cost Basis)
                c.execute(
                    "SELECT position_id, qty, avg_price_cents, cost_basis FROM positions "
                    "WHERE ticker = ? AND side = ? AND environment = ?",
                    (ticker, side, environment),
                )
                pos_row = c.fetchone()

                if pos_row:
                    pos_d = self._row_to_dict(c, pos_row)
                    old_qty = pos_d["qty"]
                    new_qty = old_qty + qty
                    new_avg = ((old_qty * pos_d["avg_price_cents"]) + (qty * price)) / new_qty
                    new_cost_basis = pos_d["cost_basis"] + ((price / 100.0) * qty)
                    c.execute(
                        """
                        UPDATE positions
                        SET qty = ?, avg_price_cents = ?, cost_basis = ?, updated_at = ?
                        WHERE position_id = ?
                        """,
                        (new_qty, new_avg, new_cost_basis, now, pos_d["position_id"]),
                    )
                    pos_id = pos_d["position_id"]
                else:
                    cost_basis = (price / 100.0) * qty
                    c.execute(
                        """
                        INSERT INTO positions (
                            ticker, side, qty, avg_price_cents, cost_basis,
                            realized_pnl_cents, unrealized_pnl_cents, updated_at,
                            environment, gumbel_mode
                        ) VALUES (?, ?, ?, ?, ?, 0.0, 0.0, ?, ?, ?)
                        """,
                        (ticker, side, qty, price, cost_basis, now, environment, gumbel_mode),
                    )
                    pos_id = self._last_id(c)

                # 3. Log Position Event
                if pos_id:
                    c.execute(
                        """
                        INSERT INTO position_events (
                            position_id, execution_id, event_type,
                            qty_change, price_cents, timestamp, environment
                        ) VALUES (?, ?, 'INCREASE', ?, ?, ?, ?)
                        """,
                        (pos_id, execution_id, qty, price, now, environment),
                    )

                conn.commit()
                return execution_id

        except pyodbc.IntegrityError:
            logger.warning(f"Duplicate execution ignored for trade_id: {exchange_trade_id}")
            return None
        except Exception as e:
            logger.error(f"Error logging execution: {e}")
            return None

    # ==========================================
    # LOGGING EXTERNAL SIGNALS (WEATHER)
    # ==========================================

    def log_weather(
        self,
        city: str,
        target_date: str,
        max_temp_f: float,
        precip_inch: float,
        is_historical: bool,
        hour: Optional[int] = None,
    ) -> Optional[int]:
        """Logs weather data for future ML inference. Unique by city, date, and hour."""
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                now = datetime.now().isoformat()
                c.execute(
                    """
                    MERGE weather_data AS target
                    USING (VALUES (?, ?, ?)) AS source (city, target_date, hour)
                        ON target.city = source.city
                       AND target.target_date = source.target_date
                       AND (target.hour = source.hour OR (target.hour IS NULL AND source.hour IS NULL))
                    WHEN MATCHED THEN
                        UPDATE SET max_temp_f = ?, precip_inch = ?, timestamp = ?, is_historical = ?
                    WHEN NOT MATCHED THEN
                        INSERT (city, target_date, hour, max_temp_f, precip_inch, timestamp, is_historical)
                        VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (city, target_date, hour,
                     max_temp_f, precip_inch, now, is_historical,
                     city, target_date, hour, max_temp_f, precip_inch, now, is_historical),
                )
                conn.commit()
                # MERGE does not support SCOPE_IDENTITY; query for the row id
                c.execute(
                    "SELECT id FROM weather_data WHERE city=? AND target_date=? "
                    "AND (hour=? OR (hour IS NULL AND ? IS NULL))",
                    (city, target_date, hour, hour),
                )
                id_row = c.fetchone()
                return int(id_row[0]) if id_row else None
        except Exception as e:
            logger.error(f"Error logging weather data for {city} on {target_date}: {e}")
            return None

    def log_prediction(
        self,
        ticker: str,
        trade_date: str,
        side: str,
        predicted_p: float,
        city: Optional[str] = None,
        horizon_hrs: Optional[float] = None,
        horizon_bin: Optional[str] = None,
        sigma: Optional[float] = None,
        ar1_correction: Optional[float] = None,
    ) -> Optional[int]:
        """Record brain's predicted P(YES) at trade time for later Brier scoring."""
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    """
                    INSERT INTO predictions
                        (ticker, trade_date, side, predicted_p, city, horizon_hrs,
                         horizon_bin, sigma, ar1_correction, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticker, trade_date, side, predicted_p, city, horizon_hrs,
                        horizon_bin, sigma, ar1_correction, datetime.now().isoformat(),
                    ),
                )
                row_id = self._last_id(c)
                conn.commit()
                return row_id
        except Exception as e:
            logger.error(f"Error logging prediction for {ticker}: {e}")
            return None

    def update_prediction_outcome(
        self, ticker: str, trade_date: str, actual_outcome: int
    ) -> int:
        """
        Write settlement result back into predictions and compute Brier score.
        actual_outcome: 1 = YES settled, 0 = NO settled.
        Returns number of rows updated.
        """
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT prediction_id, predicted_p FROM predictions "
                    "WHERE ticker = ? AND trade_date = ? AND actual_outcome IS NULL",
                    (ticker, trade_date),
                )
                rows = self._rows_to_dicts(c, c.fetchall())
                updated = 0
                for row in rows:
                    brier = (row["predicted_p"] - actual_outcome) ** 2
                    c.execute(
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

    def get_brier_summary(
        self,
        city: Optional[str] = None,
        horizon_bin: Optional[str] = None,
    ) -> dict:
        """Aggregate Brier score stats for calibration monitoring."""
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                where, params = ["actual_outcome IS NOT NULL"], []
                if city:
                    where.append("city = ?")
                    params.append(city)
                if horizon_bin:
                    where.append("horizon_bin = ?")
                    params.append(horizon_bin)
                clause = " AND ".join(where)
                c.execute(
                    f"SELECT COUNT(*) AS n, AVG(brier_score) AS avg_brier, "
                    f"MIN(brier_score) AS min_brier, MAX(brier_score) AS max_brier "
                    f"FROM predictions WHERE {clause}",
                    params,
                )
                row = c.fetchone()
                return self._row_to_dict(c, row) if row else {}
        except Exception as e:
            logger.error(f"Error fetching Brier summary: {e}")
            return {}

    def log_weather_actual(
        self,
        city: str,
        target_date: str,
        actual_temp_f: float,
        hour: Optional[int] = None,
    ) -> Optional[int]:
        """Store confirmed actual temperature from archive API (separate from forecasts)."""
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                now = datetime.now().isoformat()
                c.execute(
                    """
                    MERGE weather_actuals AS target
                    USING (VALUES (?, ?, ?)) AS source (city, target_date, hour)
                        ON target.city = source.city
                       AND target.target_date = source.target_date
                       AND (target.hour = source.hour OR (target.hour IS NULL AND source.hour IS NULL))
                    WHEN MATCHED THEN
                        UPDATE SET actual_temp_f = ?, recorded_at = ?
                    WHEN NOT MATCHED THEN
                        INSERT (city, target_date, hour, actual_temp_f, recorded_at)
                        VALUES (?, ?, ?, ?, ?);
                    """,
                    (city, target_date, hour,
                     actual_temp_f, now,
                     city, target_date, hour, actual_temp_f, now),
                )
                conn.commit()
                c.execute(
                    "SELECT id FROM weather_actuals WHERE city=? AND target_date=? "
                    "AND (hour=? OR (hour IS NULL AND ? IS NULL))",
                    (city, target_date, hour, hour),
                )
                id_row = c.fetchone()
                return int(id_row[0]) if id_row else None
        except Exception as e:
            logger.error(f"Error logging weather actual for {city} {target_date}: {e}")
            return None

    def log_ar1_residual(
        self,
        city: str,
        target_date: str,
        forecast_temp_f: float,
        actual_temp_f: float,
        horizon_hrs: Optional[float] = None,
    ) -> Optional[int]:
        """Persist (forecast, actual, error) pair for AR(1) phi estimation."""
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                error_f = actual_temp_f - forecast_temp_f
                now = datetime.now().isoformat()
                c.execute(
                    """
                    MERGE ar1_residuals AS target
                    USING (VALUES (?, ?)) AS source (city, target_date)
                        ON target.city = source.city AND target.target_date = source.target_date
                    WHEN MATCHED THEN
                        UPDATE SET forecast_temp_f = ?, actual_temp_f = ?,
                                   error_f = ?, horizon_hrs = ?, recorded_at = ?
                    WHEN NOT MATCHED THEN
                        INSERT (city, target_date, forecast_temp_f, actual_temp_f,
                                error_f, horizon_hrs, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (city, target_date,
                     forecast_temp_f, actual_temp_f, error_f, horizon_hrs, now,
                     city, target_date, forecast_temp_f, actual_temp_f, error_f, horizon_hrs, now),
                )
                conn.commit()
                c.execute(
                    "SELECT id FROM ar1_residuals WHERE city=? AND target_date=?",
                    (city, target_date),
                )
                id_row = c.fetchone()
                return int(id_row[0]) if id_row else None
        except Exception as e:
            logger.error(f"Error logging AR(1) residual for {city} {target_date}: {e}")
            return None

    def get_ar1_phi_estimate(self, city: str, min_days: int = 14) -> Optional[float]:
        """
        Estimate AR(1) coefficient phi from stored residuals using OLS:
          error_t = phi x error_{t-1} + epsilon
        Returns None if fewer than min_days data points available.
        """
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT error_f FROM ar1_residuals WHERE city = ? "
                    "ORDER BY target_date ASC",
                    (city,),
                )
                rows = c.fetchall()
            errors = [r[0] for r in rows]
            if len(errors) < min_days:
                return None
            pairs = list(zip(errors[1:], errors[:-1]))
            num = sum(e_t * e_tm1 for e_t, e_tm1 in pairs)
            den = sum(e_tm1 ** 2 for _, e_tm1 in pairs)
            return num / den if den > 0 else None
        except Exception as e:
            logger.error(f"Error estimating AR(1) phi for {city}: {e}")
            return None

    def get_sigma_mle(self, city: str, min_days: int = 14) -> Optional[float]:
        """
        MLE estimate of forecast error sigma (F) from stored AR(1) residuals:
          sigma = sqrt((1/N) sum ei^2)
        Returns None if fewer than min_days data points available.
        """
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT error_f FROM ar1_residuals WHERE city = ? "
                    "ORDER BY target_date ASC",
                    (city,),
                )
                rows = c.fetchall()
            errors = [r[0] for r in rows]
            if len(errors) < min_days:
                return None
            return math.sqrt(sum(e ** 2 for e in errors) / len(errors))
        except Exception as e:
            logger.error(f"Error computing sigma MLE for {city}: {e}")
            return None

    def get_daily_realized_pnl(
        self, environment: str, trade_date: Optional[str] = None
    ) -> float:
        """
        Sum realized P&L (cents) for all executions on trade_date.
        Returns total dollars spent today (negative = spent money).
        """
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    """
                    SELECT COALESCE(SUM(e.price_cents * e.qty), 0) AS total_spent_cents
                    FROM executions e
                    WHERE CAST(e.timestamp AS DATE) = ? AND e.environment = ?
                    """,
                    (trade_date, environment),
                )
                row = c.fetchone()
                return float(row[0]) / 100.0 if row else 0.0
        except Exception as e:
            logger.error(f"Error fetching daily P&L: {e}")
            return 0.0

    def log_orderbook_event(
        self, ticker: str, msg_type: str, payload: str, environment: str
    ) -> Optional[int]:
        """
        Persist raw orderbook snapshots/deltas as JSON payloads so they can be
        replayed in microstructure analysis and toxicity filters later.
        """
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                now = datetime.now().isoformat()
                c.execute(
                    """
                    INSERT INTO orderbook_events (ticker, msg_type, payload, timestamp, environment)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ticker, msg_type, payload, now, environment),
                )
                row_id = self._last_id(c)
                conn.commit()
                return row_id
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
                row_id = self._last_id(c)
                conn.commit()
                return row_id
        except pyodbc.IntegrityError:
            logger.warning("Duplicate sell execution ignored: %s", exchange_trade_id)
            return None
        except Exception as e:
            logger.error("Error logging sell execution record: %s", e)
            return None

    def get_open_positions(self, environment: str) -> List[Dict[str, Any]]:
        """Return positions with qty > 0 and status='open' for the given environment."""
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT * FROM positions "
                    "WHERE qty > 0 AND COALESCE(status,'open') = 'open' AND environment = ?",
                    (environment.upper(),),
                )
                return self._rows_to_dicts(c, c.fetchall())
        except Exception as e:
            logger.error(f"Error fetching open positions: {e}")
            return []

    def mark_position_settled(self, ticker: str) -> None:
        """Mark a position as settled so the monitor stops trying to exit it."""
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    "UPDATE positions SET status='settled', updated_at=? WHERE ticker=?",
                    (datetime.now().isoformat(), ticker),
                )
                conn.commit()
        except Exception as e:
            logger.error("Error marking position settled for %s: %s", ticker, e)

    def get_rolling_brier(self, n_days: int = 7) -> float:
        """Mean Brier score over settled predictions in the last n_days. Returns 0.25 if no data."""
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    """
                    SELECT AVG(brier_score) FROM predictions
                    WHERE actual_outcome IS NOT NULL
                      AND brier_score IS NOT NULL
                      AND recorded_at >= DATEADD(day, ?, CAST(GETDATE() AS DATE))
                    """,
                    (-n_days,),
                )
                row = c.fetchone()
            val = row[0] if row and row[0] is not None else 0.25
            return float(val)
        except Exception as e:
            logger.error("Error computing rolling Brier: %s", e)
            return 0.25

    def update_position_pnl(self, position_id: int, unrealized_pnl_cents: float) -> None:
        """Refresh unrealized_pnl_cents for a position from live market data."""
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    "UPDATE positions SET unrealized_pnl_cents = ?, updated_at = ? "
                    "WHERE position_id = ?",
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
        Called when a sell order fills — NOT called for settlement.
        """
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                now = datetime.now().isoformat()
                c.execute(
                    """
                    UPDATE positions
                    SET qty                  = CASE WHEN qty - ? < 0 THEN 0 ELSE qty - ? END,
                        realized_pnl_cents   = realized_pnl_cents + ?,
                        unrealized_pnl_cents = 0.0,
                        updated_at           = ?,
                        status               = CASE WHEN qty - ? <= 0 THEN 'closed' ELSE 'open' END
                    WHERE position_id = ?
                    """,
                    (qty_sold, qty_sold, realized_pnl_cents, now, qty_sold, position_id),
                )
                event_type = f"DECREASE:{exit_reason}" if exit_reason else "DECREASE"
                c.execute(
                    """
                    INSERT INTO position_events
                        (position_id, execution_id, event_type, qty_change,
                         price_cents, timestamp, environment)
                    SELECT ?, NULL, ?, ?, ?, ?, environment
                    FROM positions WHERE position_id = ?
                    """,
                    (position_id, event_type, -qty_sold, fill_price_cents, now, position_id),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Error closing position {position_id}: {e}")

    def get_rolling_brier_by_city(
        self, city: str, window: int = 30, min_obs: int = 10
    ) -> Tuple[Optional[float], int]:
        """
        Returns (avg_brier, n) for the last `window` settled predictions for a city.
        Returns (None, 0) on DB error or no data.
        """
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    """
                    SELECT TOP (?) brier_score FROM predictions
                    WHERE city = ? AND actual_outcome IS NOT NULL AND brier_score IS NOT NULL
                    ORDER BY recorded_at DESC
                    """,
                    (window, city),
                )
                rows = c.fetchall()
            n = len(rows)
            if n == 0:
                return None, 0
            avg = sum(r[0] for r in rows) / n
            return avg, n
        except Exception as e:
            logger.error("Error computing city Brier for %s: %s", city, e)
            return None, 0

    def get_tail_risk_count(
        self, city: str, window: int = 20, p_threshold: float = 0.05
    ) -> int:
        """
        Count of settled predictions in last `window` where:
          predicted_p < p_threshold AND actual_outcome = 1 (YES won).
        High count = model systematically underestimates tail risk.
        """
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    """
                    SELECT TOP (?) predicted_p, actual_outcome FROM predictions
                    WHERE city = ? AND actual_outcome IS NOT NULL
                    ORDER BY recorded_at DESC
                    """,
                    (window, city),
                )
                rows = c.fetchall()
            return sum(
                1 for r in rows if r[0] < p_threshold and r[1] == 1
            )
        except Exception as e:
            logger.error("Error computing tail risk count for %s: %s", city, e)
            return 0

    def get_canceled_order_count(
        self, ticker: str, trade_date: Optional[str] = None
    ) -> int:
        """Count canceled or timed-out orders for a ticker on trade_date (default: today UTC)."""
        if trade_date is None:
            trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    """
                    SELECT COUNT(*) FROM orders
                    WHERE ticker = ? AND status IN ('canceled', 'timeout')
                    AND CAST(created_at AS DATE) = ?
                    """,
                    (ticker, trade_date),
                )
                row = c.fetchone()
            return int(row[0]) if row else 0
        except Exception as e:
            logger.error("Error counting canceled orders for %s: %s", ticker, e)
            return 0

    def settle_position_with_outcome(self, ticker: str, yes_won: bool) -> None:
        """
        Compute and write realized P&L when Kalshi settles a market.
        yes_won=True means the YES side won (market resolved above threshold).
        """
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT position_id, side, qty, avg_price_cents FROM positions "
                    "WHERE ticker = ? AND COALESCE(status, 'open') IN ('open', 'settled')",
                    (ticker,),
                )
                rows = self._rows_to_dicts(c, c.fetchall())
                now = datetime.now().isoformat()
                for row in rows:
                    side = row["side"]
                    qty = int(row["qty"])
                    avg = float(row["avg_price_cents"])
                    if side == "yes":
                        realized = (100.0 - avg) * qty if yes_won else -avg * qty
                    else:
                        realized = (100.0 - avg) * qty if not yes_won else -avg * qty
                    c.execute(
                        """
                        UPDATE positions SET
                            realized_pnl_cents   = ?,
                            unrealized_pnl_cents = 0.0,
                            status               = 'closed',
                            updated_at           = ?
                        WHERE position_id = ?
                        """,
                        (realized, now, row["position_id"]),
                    )
                conn.commit()
                logger.info("Settled %s: yes_won=%s", ticker, yes_won)
        except Exception as e:
            logger.error("Error settling position for %s: %s", ticker, e)

    def log_trade_attribution(
        self,
        ticker: str,
        recorded_at: Optional[str] = None,
        execution_id: Optional[int] = None,
        city: Optional[str] = None,
        side: Optional[str] = None,
        horizon_bin: Optional[str] = None,
        fill_price_cents: Optional[int] = None,
        mid_at_fill_cents: Optional[int] = None,
        predicted_p: Optional[float] = None,
        market_implied_p: Optional[float] = None,
        realized_outcome: Optional[int] = None,
        expected_value_cents: Optional[float] = None,
        realized_pnl_cents: Optional[float] = None,
        slippage_cents: Optional[float] = None,
        fees_cents: Optional[float] = None,
        holding_time_hrs: Optional[float] = None,
    ) -> Optional[int]:
        """
        Record a per-fill PnL attribution row decomposing the trade result into:
        model alpha, slippage, fees, and realized outcome.
        All nullable fields default to None when unknown at call time.
        """
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                ts = recorded_at or datetime.now().isoformat()
                c.execute(
                    """
                    INSERT INTO trade_attribution (
                        execution_id, ticker, city, side, horizon_bin,
                        fill_price_cents, mid_at_fill_cents,
                        predicted_p, market_implied_p, realized_outcome,
                        expected_value_cents, realized_pnl_cents,
                        slippage_cents, fees_cents, holding_time_hrs,
                        recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        execution_id, ticker, city, side, horizon_bin,
                        fill_price_cents, mid_at_fill_cents,
                        predicted_p, market_implied_p, realized_outcome,
                        expected_value_cents, realized_pnl_cents,
                        slippage_cents, fees_cents, holding_time_hrs,
                        ts,
                    ),
                )
                row_id = self._last_id(c)
                conn.commit()
                return row_id
        except Exception as e:
            logger.error(f"Error logging trade attribution for {ticker}: {e}")
            return None

    def log_calibration_diagnostic(
        self,
        ts: str,
        ticker: str,
        city: Optional[str],
        horizon_bucket: Optional[str],
        strike_distance_bucket: Optional[str],
        p_model: Optional[float],
        p_market: Optional[float],
        edge: Optional[float],
        trade_side: Optional[str],
        gumbel_mode: Optional[str],
        env_mode: Optional[str],
    ) -> Optional[int]:
        """Insert one calibration diagnostic row. Returns lastrowid or None on error."""
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    """
                    INSERT INTO calibration_diagnostics (
                        ts, ticker, city, horizon_bucket, strike_distance_bucket,
                        p_model, p_market, edge, trade_side, gumbel_mode, env_mode
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts, ticker, city, horizon_bucket, strike_distance_bucket,
                        p_model, p_market, edge, trade_side, gumbel_mode, env_mode,
                    ),
                )
                row_id = self._last_id(c)
                conn.commit()
                return row_id
        except Exception as e:
            logger.error(f"Error logging calibration diagnostic for {ticker}: {e}")
            return None

    def get_city_edge_summary(
        self, city: str, n_days: int = 7, min_n: int = 20
    ) -> Tuple[float, int]:
        """
        Returns (avg_edge, count) from calibration_diagnostics for the given city
        over the last n_days. Returns (0.0, count) when count < min_n.
        """
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    """
                    SELECT AVG(edge) AS avg_edge, COUNT(*) AS n
                    FROM calibration_diagnostics
                    WHERE city = ?
                      AND ts >= CAST(DATEADD(day, ?, CAST(GETDATE() AS DATE)) AS NVARCHAR)
                    """,
                    (city, -n_days),
                )
                row = c.fetchone()
                d = self._row_to_dict(c, row) if row else {}
            count = int(d["n"]) if d.get("n") is not None else 0
            if count < min_n or count == 0:
                return 0.0, count
            avg_edge = float(d["avg_edge"]) if d.get("avg_edge") is not None else 0.0
            return avg_edge, count
        except Exception as e:
            logger.error(f"Error fetching city edge summary for {city}: {e}")
            return 0.0, 0

    def upsert_experiment_run(
        self,
        run_date: str,
        gumbel_mode: str,
        total_trades: int,
        yes_trades: int,
        no_trades: int,
        avg_edge_cents: Optional[float],
        avg_lvr_cents: Optional[float],
        realized_pnl_cents: Optional[float],
        brier_score: Optional[float],
        n_settled: int,
    ) -> Optional[int]:
        """
        Insert or update an experiment_runs row keyed on (run_date, gumbel_mode).
        Returns the row id or None on error.
        """
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                now = datetime.now().isoformat()
                c.execute(
                    """
                    MERGE experiment_runs AS target
                    USING (VALUES (?, ?)) AS source (run_date, gumbel_mode)
                        ON target.run_date = source.run_date
                       AND target.gumbel_mode = source.gumbel_mode
                    WHEN MATCHED THEN
                        UPDATE SET total_trades       = ?,
                                   yes_trades         = ?,
                                   no_trades          = ?,
                                   avg_edge_cents     = ?,
                                   avg_lvr_cents      = ?,
                                   realized_pnl_cents = ?,
                                   brier_score        = ?,
                                   n_settled          = ?,
                                   recorded_at        = ?
                    WHEN NOT MATCHED THEN
                        INSERT (run_date, gumbel_mode, total_trades, yes_trades, no_trades,
                                avg_edge_cents, avg_lvr_cents, realized_pnl_cents,
                                brier_score, n_settled, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        run_date, gumbel_mode,
                        total_trades, yes_trades, no_trades,
                        avg_edge_cents, avg_lvr_cents, realized_pnl_cents,
                        brier_score, n_settled, now,
                        run_date, gumbel_mode, total_trades, yes_trades, no_trades,
                        avg_edge_cents, avg_lvr_cents, realized_pnl_cents,
                        brier_score, n_settled, now,
                    ),
                )
                conn.commit()
                c.execute(
                    "SELECT id FROM experiment_runs WHERE run_date=? AND gumbel_mode=?",
                    (run_date, gumbel_mode),
                )
                id_row = c.fetchone()
                return int(id_row[0]) if id_row else None
        except Exception as e:
            logger.error(f"Error upserting experiment run for {run_date}/{gumbel_mode}: {e}")
            return None

    # ------------------------------------------------------------------
    # BOT CONFIG — runtime key/value store (hot-swap without restart)
    # ------------------------------------------------------------------

    def get_config(self, key: str, default: Optional[str] = None) -> Optional[str]:
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT value FROM bot_config WHERE config_key = ?", (key,))
                row = c.fetchone()
                return row[0] if row else default
        except Exception as e:
            logger.error("get_config(%s) failed: %s", key, e)
            return default

    def set_config(self, key: str, value: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self.get_connection() as conn:
                c = conn.cursor()
                c.execute(
                    """
                    MERGE bot_config AS tgt
                    USING (SELECT ? AS config_key, ? AS value, ? AS updated_at) AS src
                    ON tgt.config_key = src.config_key
                    WHEN MATCHED THEN
                        UPDATE SET value = src.value, updated_at = src.updated_at
                    WHEN NOT MATCHED THEN
                        INSERT (config_key, value, updated_at)
                        VALUES (src.config_key, src.value, src.updated_at);
                    """,
                    (key, value, now),
                )
                conn.commit()
                logger.info("bot_config: %s = %s", key, value)
        except Exception as e:
            logger.error("set_config(%s, %s) failed: %s", key, value, e)
