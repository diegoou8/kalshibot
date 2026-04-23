# /project:analyze-market

Scan active Kalshi weather markets and identify the best trading opportunities ranked by expected value.

## Steps
1. Read `src/services/kalshi_client.py` to understand `get_weather_markets()` and `get_market(ticker)`
2. Read `src/layer2/ev_engine.py` to use `KalshiFeeModel` and `EVEngine`
3. Read `src/decision/engine.py` to understand current arbitrage rules
4. For each active weather market:
   - Check if `yes_ask + no_ask < 100` (pure arbitrage)
   - Calculate EV using `EVEngine` for both YES and NO sides
   - Pull latest weather signal from `weather_data` table for that city
5. Rank top 5 opportunities by net EV after fees
6. Check `data/DWTrader.db` via `check_data.py` for recent scan history

## Output Format
```
=== MARKET SCAN — {timestamp} ===

[ARBITRAGE] HIGHNY-2025-01-15-T65
  YES Ask: 45¢  NO Ask: 50¢  Sum: 95¢  Edge: 5¢
  Net EV: +3.2¢/contract after fees (10 contracts = +$0.32)
  Weather: New York forecast 67°F vs strike 65°F → YES favored

[STATISTICAL] HIGHDFW-2025-01-15-T72
  YES Ask: 82¢  EV: +1.8¢ (Kelly: 3 contracts)
  Weather: Dallas forecast 74°F vs strike 72°F → slight YES edge

No more opportunities above EV threshold (3¢ minimum)

Closest non-qualifying markets:
  HIGHCHI-2025-01-15-T45: sum=103¢, EV=-1.2¢ (overpriced)
```

## Notes
- Demo API only — never scan live API without authorization
- Focus on weather markets only (category: Climate and Weather)
- EV threshold: 3¢/contract minimum (matches Gate 1 in gating_logic.py)
- If weather data is stale (> 2 hours old), flag it in output
