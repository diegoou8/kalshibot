# Safety Protocol — Trading Bot

## Hard Rules (Never Break)
1. NEVER submit live orders unless `ENV_MODE=live` is explicitly set in `.env`
2. NEVER set `ENV_MODE=live` without explicit user authorization in the conversation
3. NEVER exceed `MAX_POSITION_SIZE=$50` per trade
4. NEVER exceed `DAILY_LOSS_LIMIT=$250` per day
5. ALWAYS run paper mode first for any new strategy or code change
6. NEVER commit credential files (Credentials/ is gitignored — keep it that way)

## Credential Safety
- Use `DiegoDemoKey.txt` for all development, testing, and paper trading
- `DiegoAPIKey.txt` / `DiegoAPIKEYv2.txt` (live keys) — only reference when user explicitly authorizes live trading
- Never print or log private key file contents
- Never hardcode key IDs or paths in source files — use env vars or config

## Order Submission Checklist
Before any code path that calls `kalshi_client.submit_order()`, verify:
1. `ENV_MODE` is checked — if `paper`, skip submission and simulate fill
2. `preflight_check()` passed with no rejections
3. Position size within `MAX_POSITION_SIZE`
4. Daily limits not breached (volume + loss)
5. Intent logged to DB before submission (audit trail)

## Paper Mode Behavior
When `ENV_MODE=paper`:
- Decision engine runs with real signals and real market data
- Risk manager runs all checks normally
- ExecutionManager simulates the fill at intent price (no API call)
- All pipeline steps logged to DB (full audit trail, `source='paper'`)
- Position table updated as if real (for paper PnL tracking)

## Halt Protocol
If `data/halt.flag` exists → all trading stops immediately.
Halt is set automatically by: daily loss circuit breaker.
Halt is cleared manually only: `python scripts/clear_halt.py --reason "reviewed, resuming"`

## Incident Response
If daily loss limit is breached:
1. Circuit breaker sets halt flag automatically
2. Log halt event to decision_log with reason
3. Do not restart without manual review of what went wrong
4. Check: which markets lost, was it a bug or a bad trade?
