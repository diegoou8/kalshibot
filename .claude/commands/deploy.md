# /project:deploy

Deploy the trading bot via Docker (ingest service + trading loop).

## Pre-deployment Checklist
Before deploying, confirm ALL of the following:
1. [ ] `pytest tests/` — all pass
2. [ ] `/project:health-check` — all green
3. [ ] `/project:backtest` — run and reviewed (Sharpe > 1.0, drawdown acceptable)
4. [ ] `/project:run-paper` — at least 1 hour paper session reviewed
5. [ ] `ENV_MODE` confirmed (paper or live — user must explicitly authorize live)
6. [ ] `docker-compose.yml` and `Dockerfile-ingest` are up to date

## LIVE TRADING AUTHORIZATION
If deploying with `ENV_MODE=live`:
- Stop and explicitly confirm with the user: "You are about to deploy LIVE trading. Confirm?"
- Only proceed after explicit "yes" in the conversation
- Double-check: `DiegoAPIKey.txt` (not DemoKey) is configured
- Capital limits are correct in `.env`

## Deployment Steps
```bash
# 1. Build the ingest container
docker-compose build

# 2. Start ingest service (WebSocket + weather) — always first
docker-compose up -d layer1-ingest

# 3. Monitor ingest logs for 2 minutes before starting trader
docker-compose logs -f layer1-ingest

# 4. Verify DB is receiving data (scans table has new rows)
python check_data.py

# 5. Start trading loop (only after ingest is confirmed working)
python -m src.index  # or deploy as second Docker container
```

## Post-deployment Monitoring
For first 30 minutes:
- Watch `docker-compose logs -f` for any errors
- Run `/project:check-positions` after 15 minutes
- Run `/project:health-check` after 30 minutes
- If any issue: `docker-compose down` immediately

## Rollback
```bash
docker-compose down
# Investigate DB state with check_data.py
# Fix issue, re-run pre-deployment checklist before redeploying
```

## Notes
- ALWAYS deploy ingest service before trading loop
- Ingest service can run 24/7 safely — no orders, just data collection
- Trading loop can be stopped/started independently without data loss
