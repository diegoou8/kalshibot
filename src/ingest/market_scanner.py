import asyncio
import logging
import time

from ..db.dwtrader import DWTraderDB
from ..services.kalshi_client import client
from ..analysis.arbitrage import ArbitrageAnalyzer
from ..config.env import Config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] - %(message)s',
                    handlers=[logging.FileHandler("kalshi-bot-python/market_crons.log", encoding="utf-8"),
                              logging.StreamHandler()])

logger = logging.getLogger("MarketScannerCron")

async def scan_market_tick():
    """Single tick to scan markets exactly ONCE. Designed for CRON every 30 secs."""
    logger.info("📡 Scanning Kalshi Markets for 30s TICK...")
    db = DWTraderDB()
    env_mode = "PAPER" # Set to config when required
    analyzer = ArbitrageAnalyzer()
    
    markets = await client.get_active_markets(limit=50)
    
    if not markets:
         logger.warning("No active markets returned. Sandbox might be paused.")
         return
         
    logger.info(f"Retrieved {len(markets)} active markets. Updating scans table.")
    
    for market in markets:
         ticker = market.get('ticker')
         yes_ask = market.get('yes_ask', 100)
         no_ask = market.get('no_ask', 100)
         yes_bid = market.get('yes_bid', 0)
         no_bid = market.get('no_bid', 0)
         volume = market.get('volume', 0)
         
         # Skip markets that don't have active quotes
         if yes_ask == 100 and no_ask == 100:
              continue
              
         # DB Log: Market Scan
         spread = yes_ask - yes_bid
         scan_id = db.log_scan(
             ticker=ticker, market_prob=yes_ask/100.0, ml_prob=0.0,
             best_bid=yes_bid, best_ask=yes_ask, spread=spread, volume=volume, environment=env_mode
         )
         
         # Arbitrage Analysis & Decision
         arb_result = analyzer.analyze_spread(market)
         db.log_decision(
             scan_id=scan_id, expected_value=0.0, kelly_fraction=0.0, risk_score=0.0,
             ml_prob=0.0, arb_signal=arb_result.get('type') if arb_result.get('type') else "none",
             decision="SUBMIT" if arb_result.get('is_arb') else "SKIP", environment=env_mode
         )
         
    logger.info("✅ TICK complete. Scans safely persisted.")

if __name__ == "__main__":
    # Usually you'd trigger this exactly once per execution in standard OS CRON
    # Example Linux Cron setting (runs every minute and runs script twice physically if needed, or loop inside)
    # Since OS Cron is min interval 1-Minute: To get 30s we run a tiny 2-tick loop
    async def _runner():
         await scan_market_tick()
         logger.info("💤 Sleeping for 30s (CRON Emulation)...")
         await asyncio.sleep(30)
         await scan_market_tick()
         
    asyncio.run(_runner())
