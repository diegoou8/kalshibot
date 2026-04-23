"""
End-to-end connection tests:
  1. Weather ingestion (Open-Meteo → SQLite)
  2. Kalshi REST (balance, weather markets, orderbook)
  3. Kalshi demo order (1-contract limit order, safe low price)

Run from project root:
    python scripts/test_connections.py
"""
import asyncio
import logging
import sys
import json
import io
from pathlib import Path

# Force UTF-8 output on Windows so emoji/box-drawing chars don't crash
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── make src importable from project root ──────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.services.kalshi_client import KalshiClient
from src.ingest.weather import WeatherIngestor
from src.db.dwtrader import DWTraderDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

PASS = "✅ PASS"
FAIL = "❌ FAIL"
SEP  = "─" * 60


# ══════════════════════════════════════════════════════════════════════════════
# TEST 1 — WEATHER INGESTION
# ══════════════════════════════════════════════════════════════════════════════

async def test_weather(db: DWTraderDB) -> bool:
    print(f"\n{SEP}")
    print("TEST 1 — Weather Ingestion (Open-Meteo → SQLite)")
    print(SEP)

    ingestor = WeatherIngestor(db)
    city = "NEW YORK"

    try:
        print(f"  Fetching 7-day forecast for {city}...")
        await ingestor.ingest_forecast_data(city)

        # Verify rows landed in DB
        import sqlite3
        conn = sqlite3.connect(db.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT COUNT(*) as cnt, MIN(target_date) as min_d, MAX(target_date) as max_d "
            "FROM weather_data WHERE city = ? AND is_historical = 0",
            (city,)
        ).fetchone()
        conn.close()

        cnt = rows["cnt"]
        if cnt > 0:
            print(f"  Rows in weather_data for {city}: {cnt}")
            print(f"  Date range: {rows['min_d']} → {rows['max_d']}")
            print(f"  {PASS}  Weather ingestion working")
            return True
        else:
            print(f"  {FAIL}  No rows written to weather_data")
            return False

    except Exception as e:
        print(f"  {FAIL}  Exception: {e}")
        logger.exception("Weather ingestion failed")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# TEST 2 — KALSHI REST
# ══════════════════════════════════════════════════════════════════════════════

async def test_kalshi_rest(client: KalshiClient) -> tuple[bool, str | None, int | None]:
    """Returns (passed, ticker_with_active_quotes, yes_ask_cents)."""
    print(f"\n{SEP}")
    print("TEST 2 — Kalshi REST (balance + weather markets)")
    print(SEP)

    all_passed = True
    found_ticker = None
    found_ask = None

    # 2a — balance
    try:
        balance = await client.get_balance()
        if balance > 0:
            print(f"  Portfolio balance: ${balance:,.2f}")
            print(f"  {PASS}  Auth & balance endpoint working")
        else:
            print(f"  Balance returned 0 — may be empty demo account or auth issue")
            print(f"  {FAIL}  Balance check")
            all_passed = False
    except Exception as e:
        print(f"  {FAIL}  Balance call raised: {e}")
        all_passed = False

    # 2b — weather markets
    try:
        print("\n  Scanning weather series & open markets (may take ~10s)...")
        markets = await client.get_weather_markets()
        if markets:
            print(f"  Found {len(markets)} open weather market(s)")

            # Find first market with active quotes (yes_ask not None)
            with_quotes = [m for m in markets if m.get("yes_ask") is not None]
            print(f"  Markets with active yes_ask quotes: {len(with_quotes)}")

            # Show sample of first 5 (any)
            for m in markets[:5]:
                print(f"    {m.get('ticker','?'):40s}  yes_ask={m.get('yes_ask')}  no_ask={m.get('no_ask')}")

            if with_quotes:
                best = with_quotes[0]
                found_ticker = best.get("ticker")
                found_ask    = best.get("yes_ask")
                print(f"\n  Best quoted market: {found_ticker}  yes_ask={found_ask}¢")
            else:
                # No active asks — still pick the first market; order test will use 1¢ resting limit
                found_ticker = markets[0].get("ticker")
                found_ask    = 50  # placeholder — order will be at 1¢ anyway
                print(f"\n  No active asks in demo — using {found_ticker} for order test (resting limit)")

            print(f"  {PASS}  Weather market fetch working")
        else:
            print(f"  No open weather markets found")
            print(f"  {FAIL}  Weather markets returned empty list")
            all_passed = False
    except Exception as e:
        print(f"  {FAIL}  Weather markets raised: {e}")
        logger.exception("Weather market fetch failed")
        all_passed = False

    # 2c — if we found a quoted ticker, verify market detail endpoint
    check_ticker = found_ticker or (markets[0].get("ticker") if markets else None)
    if check_ticker:
        try:
            market = await client.get_market(check_ticker)
            if market:
                print(f"\n  Market detail for {check_ticker}:")
                # Print all non-None fields for discovery
                for k, v in market.items():
                    if v is not None:
                        print(f"    {k}: {v}")
                print(f"  {PASS}  Market detail endpoint working")
            else:
                print(f"  {FAIL}  get_market returned empty for {check_ticker}")
                all_passed = False
        except Exception as e:
            print(f"  {FAIL}  get_market raised: {e}")
            all_passed = False

    return all_passed, found_ticker, found_ask


# ══════════════════════════════════════════════════════════════════════════════
# TEST 3 — DEMO ORDER PLACEMENT
# ══════════════════════════════════════════════════════════════════════════════

async def test_order(client: KalshiClient, db: DWTraderDB, ticker: str, yes_ask: int) -> bool:
    print(f"\n{SEP}")
    print("TEST 3 — Demo Order Placement")
    print(SEP)
    print(f"  Ticker: {ticker}")

    try:
        # Submit a resting limit at 1¢ — valid order, safe for demo, won't fill
        order_price = 1
        ask_display = f"{yes_ask}¢" if yes_ask else "no active ask"
        print(f"  Current yes_ask: {ask_display}  →  Submitting limit buy at {order_price}¢ (1 contract)")
        print("  (Resting limit — will not fill, just proves order submission works)")

        result = await client.submit_order(
            ticker=ticker,
            action="buy",
            side="yes",
            count=1,
            price_cents=order_price,
        )

        if result.get("status") == "submitted":
            order = result.get("order", {})
            order_id = order.get("order_id", order.get("client_order_id", "?"))
            status   = order.get("status", "?")
            print(f"\n  Exchange order_id: {order_id}")
            print(f"  Order status:      {status}")

            # Persist to DB (intent_id=None — no parent intent in test context)
            db_order_id = db.log_order(
                intent_id=None,
                exchange_order_id=str(order_id),
                ticker=ticker,
                side="yes",
                price=order_price,
                qty=1,
                order_type="limit",
                status=status,
                environment="PAPER",
            )
            print(f"  DB order_id:       {db_order_id}")
            print(f"  {PASS}  Order submitted and logged to DB")
            return True
        else:
            err = result.get("error", "unknown")
            print(f"  {FAIL}  Order rejected: {err}")
            return False

    except Exception as e:
        print(f"  {FAIL}  Order raised: {e}")
        logger.exception("Order placement failed")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    print("=" * 60)
    print("  KALSHI BOT — CONNECTION TEST SUITE")
    print("=" * 60)

    db     = DWTraderDB()
    client = KalshiClient()

    if not client._private_key:
        print(f"\n{FAIL}  RSA key failed to load — check Credentials/DiegoDemoKey.txt")
        sys.exit(1)
    print(f"\n  RSA key loaded ✓  (key_id: {__import__('src.config.env', fromlist=['Config']).Config.KALSHI_DEMO_KEY_ID})")

    results = {}

    results["weather"]                   = await test_weather(db)
    rest_ok, first_ticker, first_ask     = await test_kalshi_rest(client)
    results["kalshi_rest"]               = rest_ok

    if first_ticker:
        results["order"] = await test_order(client, db, first_ticker, first_ask or 0)
    else:
        print(f"\n{SEP}")
        print("TEST 3 — Demo Order Placement")
        print(SEP)
        print(f"  SKIPPED — no weather ticker available from TEST 2")
        results["order"] = False

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("  SUMMARY")
    print("=" * 60)
    for name, passed in results.items():
        icon = PASS if passed else FAIL
        print(f"  {icon}  {name}")

    all_ok = all(results.values())
    print("=" * 60)
    if all_ok:
        print("  ALL TESTS PASSED — ready to wire up the brain 🚀")
    else:
        print("  SOME TESTS FAILED — see output above for details")
    print("=" * 60)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
