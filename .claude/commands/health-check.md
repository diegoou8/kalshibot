# /project:health-check

Verify all system components are working correctly before starting a trading session.

## Checks to Run

### Layer 0: Configuration
- [ ] `.env` file exists at project root
- [ ] `ENV_MODE` is set (warn if missing — defaults to paper)
- [ ] Capital limits configured: `BANKROLL`, `MAX_POSITION_SIZE`, `DAILY_LOSS_LIMIT`
- [ ] `Credentials/DiegoDemoKey.txt` exists and is readable

### Layer 1: API Connections
- [ ] Kalshi REST reachable: `GET /trade-api/v2/exchange/status` → 200
- [ ] Kalshi auth valid: `GET /trade-api/v2/portfolio/balance` → 200 (shows balance)
- [ ] Open-Meteo reachable: test one city → 200
- [ ] WebSocket connects and receives one message within 10 seconds

### Layer 2: Database
- [ ] `data/DWTrader.db` exists
- [ ] All 9 tables present (scans, decision_log, intents, orders, executions, positions, position_events, weather_data, orderbook_events)
- [ ] DB is writable (insert test row and rollback)
- [ ] No stale WAL journal (`data/DWTrader.db-wal` > 100MB = warning)
- [ ] Last scan timestamp < 10 minutes ago (if ingest is supposed to be running)

### Layer 3: Math
- [ ] Run `pytest tests/` — all tests pass
- [ ] Fee formula spot check: 10 contracts at 50¢ → fee = 18¢
- [ ] EV check: yes_ask=45, no_ask=50 → net EV > 0

### Layer 4: Safety
- [ ] `data/halt.flag` does NOT exist (or explain why it does)
- [ ] `ENV_MODE=paper` confirmed (not live) — unless user authorizes live

## Output Format
```
=== HEALTH CHECK — {timestamp} ===

Config:        ✅ .env found, ENV_MODE=paper
Credentials:   ✅ DiegoDemoKey.txt readable
Kalshi API:    ✅ reachable (142ms), auth valid, balance: $2,500.00
Open-Meteo:    ✅ reachable (89ms)
WebSocket:     ✅ connected, received market_snapshot in 3.1s
Database:      ✅ 9 tables, last scan 4m ago, WAL=2.1MB
Math tests:    ✅ 8/8 pass
Fee formula:   ✅ 18¢ (correct)
Halt flag:     ✅ not set
ENV_MODE:      ✅ paper (safe to trade)

Overall: READY ✅
```

If any check fails, explain what's wrong and how to fix it.
