---
name: arbitrage-scan
description: Scan Kalshi weather markets for arbitrage opportunities where yes_ask + no_ask < 100, ranked by net EV after fees
---

When invoked, perform a live arbitrage scan:

1. Read `src/services/kalshi_client.py` — use `get_weather_markets()` to get active tickers
2. Read `src/layer2/ev_engine.py` — use `KalshiFeeModel` and `EVEngine` for net EV calculation
3. Read `src/decision/engine.py` — understand current arbitrage detection logic
4. For each weather market ticker:
   a. Fetch current orderbook (bid/ask for YES and NO)
   b. Calculate: `arb_sum = yes_ask + no_ask`
   c. If `arb_sum < 100`: pure arbitrage found — edge = `100 - arb_sum` cents
   d. Calculate net EV after Kalshi fees for 10 contracts
   e. Check weather signal: is forecast favoring YES or NO?
5. Rank all opportunities by net EV descending
6. Report top 5, then list closest non-qualifying markets

Output format:
```
ARBITRAGE SCAN — {ISO timestamp}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[ARB] HIGHNY-2025-01-15-T65
  YES Ask: 45¢  NO Ask: 50¢  Sum: 95¢  Raw Edge: 5¢
  Net EV (10 contracts): +$0.32 after fees
  Weather: NYC forecast 67°F vs strike 65°F → YES favored

[STAT] HIGHDFW-2025-01-15-T72  (statistical, no pure arb)
  YES Ask: 82¢  NO Ask: 22¢  Sum: 104¢  (overpriced by 4¢)
  Weather EV: +1.8¢ if forecast is correct (prob=0.84)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scanned: {N} markets  Arb found: {M}  Best non-arb EV: +{X}¢
```

If no arbitrage exists, report the closest markets (lowest sum) and explain why they don't qualify.
Use demo API only — never scan live endpoints without authorization.
