# /project:run-paper

Start a paper trading session — real market signals, real decisions, no live orders submitted.

## Pre-flight Checks
Before starting, verify:
1. `.env` exists at project root with `ENV_MODE=paper`
2. `Credentials/DiegoDemoKey.txt` exists and is readable
3. `data/DWTrader.db` exists and is writable (or will be created)
4. `data/halt.flag` does NOT exist (if it does, warn and stop)
5. No `ingest_app.py` process already running (check with `ps aux | grep ingest`)

## Start Sequence
1. Read `src/config/env.py` to confirm all required env vars
2. Read `ingest_app.py` to understand the ingestion entry point
3. Read `src/index.py` to understand the trading loop
4. Confirm `ENV_MODE=paper` in the running config
5. Start session:
   ```bash
   # Terminal 1: Start ingest (WebSocket + weather)
   python ingest_app.py
   
   # Terminal 2: Start trading loop
   python -m src.index
   ```

## Monitoring During Session
After 5 minutes, check:
- `data/DWTrader.db` scans table has new rows (ingest working)
- `data/DWTrader.db` weather_data has recent entries
- decision_log has entries (decisions being made)
- If `ENV_MODE=paper`: orders table should show `source='paper'`

## Session Report (after stopping)
Run `check_data.py` and report:
- Total scans processed
- Signals found vs rejected (by reason)
- Paper trades placed + simulated P&L
- Any errors encountered

## Notes
- Paper mode runs the full pipeline — decisions, risk checks, DB logging — just skips the actual API order call
- Run for at least 1 hour before evaluating signal quality
- Compare decision_log reasons: if most rejections are "MAX_POSITION_SIZE", tune intent quantity
