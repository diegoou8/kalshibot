"""Backfill trade_attribution for existing executions using calibration_diagnostics."""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
import pyodbc

conn = pyodbc.connect(os.environ["AZURE_SQL_CONN_STR"])
c = conn.cursor()

c.execute("""
    SELECT
        e.execution_id,
        e.price_cents,
        e.qty,
        e.timestamp            AS fill_ts,
        o.ticker,
        o.side,
        o.gumbel_mode,
        cd.city,
        cd.horizon_bucket,
        cd.p_model,
        cd.p_market,
        cd.edge
    FROM executions e
    JOIN orders o ON o.order_id = e.order_id
    LEFT JOIN trade_attribution ta ON ta.execution_id = e.execution_id
    OUTER APPLY (
        SELECT TOP 1
            city, horizon_bucket, p_model, p_market, edge
        FROM calibration_diagnostics
        WHERE ticker = o.ticker
          AND ts <= CONVERT(NVARCHAR, e.timestamp, 126)
        ORDER BY ts DESC
    ) cd
    WHERE ta.attribution_id IS NULL
""")
cols = [d[0] for d in c.description]
rows = [dict(zip(cols, r)) for r in c.fetchall()]
print(f"Found {len(rows)} executions to backfill")

now = datetime.utcnow().isoformat()
inserted = 0
for r in rows:
    ev = None
    if r["p_model"] is not None and r["price_cents"] is not None:
        if r["side"] == "yes":
            ev = r["p_model"] * (100 - r["price_cents"]) - (1 - r["p_model"]) * r["price_cents"]
        else:
            p_no = 1.0 - r["p_model"]
            ev = p_no * (100 - r["price_cents"]) - (1 - p_no) * r["price_cents"]

    c.execute(
        """
        INSERT INTO trade_attribution
            (execution_id, ticker, city, side, horizon_bin,
             fill_price_cents, predicted_p, market_implied_p,
             expected_value_cents, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            r["execution_id"], r["ticker"], r["city"], r["side"],
            r["horizon_bucket"], r["price_cents"],
            r["p_model"], r["p_market"],
            ev, now,
        ),
    )
    inserted += 1

conn.commit()
print(f"Inserted {inserted} rows into trade_attribution")

# Summary by city
c.execute("""
    SELECT
        city,
        COUNT(*) AS n_fills,
        SUM(CASE WHEN side='yes' THEN 1 ELSE 0 END) AS yes_fills,
        SUM(CASE WHEN side='no'  THEN 1 ELSE 0 END) AS no_fills,
        ROUND(AVG(CAST(fill_price_cents AS FLOAT)), 1) AS avg_price_c,
        ROUND(AVG(predicted_p), 4) AS avg_p_model,
        ROUND(AVG(market_implied_p), 4) AS avg_p_market,
        ROUND(AVG(expected_value_cents), 2) AS avg_ev_c
    FROM trade_attribution
    GROUP BY city
    ORDER BY n_fills DESC
""")
cols = [d[0] for d in c.description]
print("\n--- Attribution by city ---")
for row in c.fetchall():
    print(dict(zip(cols, row)))

# Summary by mode (join to orders for gumbel_mode)
c.execute("""
    SELECT
        o.gumbel_mode,
        COUNT(*) AS n_fills,
        SUM(CASE WHEN o.side='yes' THEN 1 ELSE 0 END) AS yes_fills,
        SUM(CASE WHEN o.side='no'  THEN 1 ELSE 0 END) AS no_fills,
        ROUND(AVG(ta.expected_value_cents), 2) AS avg_ev_c,
        ROUND(AVG(ta.fill_price_cents), 1) AS avg_price_c
    FROM trade_attribution ta
    JOIN executions e ON e.execution_id = ta.execution_id
    JOIN orders o ON o.order_id = e.order_id
    GROUP BY o.gumbel_mode
    ORDER BY n_fills DESC
""")
cols = [d[0] for d in c.description]
print("\n--- Attribution by gumbel_mode ---")
for row in c.fetchall():
    print(dict(zip(cols, row)))

# Summary by city x mode
c.execute("""
    SELECT
        ta.city,
        o.gumbel_mode,
        COUNT(*) AS n_fills,
        SUM(CASE WHEN o.side='yes' THEN 1 ELSE 0 END) AS yes_fills,
        SUM(CASE WHEN o.side='no'  THEN 1 ELSE 0 END) AS no_fills,
        ROUND(AVG(ta.expected_value_cents), 2) AS avg_ev_c,
        ROUND(AVG(ta.fill_price_cents), 1) AS avg_price_c,
        ROUND(AVG(ta.predicted_p), 4) AS avg_p_model
    FROM trade_attribution ta
    JOIN executions e ON e.execution_id = ta.execution_id
    JOIN orders o ON o.order_id = e.order_id
    GROUP BY ta.city, o.gumbel_mode
    ORDER BY ta.city, o.gumbel_mode
""")
cols = [d[0] for d in c.description]
print("\n--- Attribution by city x mode ---")
for row in c.fetchall():
    print(dict(zip(cols, row)))

conn.close()
