# Kalshi Weather Arbitrage Bot

## Project Overview
Automated trading bot targeting weather-linked prediction markets on Kalshi. Uses arbitrage detection + ML probability estimation to find edges in temperature/weather markets. The "brain" (ML model) is a pluggable module that can be connected or disconnected from the execution pipeline without changing any other layer.

## Architecture — 5 Layers

### Layer 0: Config & Types
- `src/config/env.py` — Credentials, capital limits, 19 city coordinates
- `src/types.py` — Type definitions (Coin, Minutes, MarketConfig)
- `src/constant/index.py` — Constants (channel names, minBuyUSD=$1.00)

### Layer 1: Data Ingestion (always running)
- `src/services/kalshi_client.py` — REST + WebSocket API client (RSA-PSS auth)
- `src/ingest/ws_client.py` — WebSocket pipeline, auto-reconnect, orderbook streaming
- `src/ingest/weather.py` — Open-Meteo weather ingestion (historical + 7-day forecast)
- `ingest_app.py` — Entry point: runs both WebSocket + weather as concurrent async tasks

### Layer 2: Quant Models (the math)
- `src/decision/engine.py` — Basic arbitrage + statistical rules → TradeIntent
- `src/layer2/ev_engine.py` — EV with official Kalshi fee model + slippage
- `src/layer2/particle_filter.py` — SMC particle filter over temperature distribution
- `src/layer2/gating_logic.py` — 8-gate trade execution filter
- `src/layer2/pipeline.py` — Event coordinator (Stream A: forecasts, Stream B: markets)

### Layer 3: Risk & Execution
- `src/risk/manager.py` — Preflight checks, position limits, daily loss guard
- `src/execution/manager.py` — Order sniper with live depth check before submit
- `src/logging/trade_logger.py` — DB wrapper isolating business logic from persistence

### Layer 4: The Brain (ML — pluggable)
- Currently: `src/decision/engine.py` uses hardcoded `prob_win=0.98`
- Target: ML model implementing `BrainModel` protocol, injected via `DecisionEngine(brain=...)`
- When brain=None, engine falls back to rule-based defaults
- Brain artifacts stored in `data/models/`

## Database — Azure SQL (production), pyodbc + AZURE_SQL_CONN_STR
| Table | Purpose |
|---|---|
| scans | Market observations (price, spread, volume) |
| decision_log | Math engine output per scan |
| intents | Pre-execution trade intentions |
| orders | Submitted orders (includes gumbel_mode column) |
| executions | Order fills (UNIQUE on order_id + trade_id) |
| positions | Active positions with running PnL (includes gumbel_mode) |
| position_events | Audit log for all position changes |
| weather_data | Historical + forecast weather signals |
| weather_actuals | Open-Meteo archive actuals (for AR(1) calibration) |
| orderbook_events | Raw orderbook snapshots/deltas (JSON, for replay) |
| predictions | Per-prediction log with Brier score after settlement |
| ar1_residuals | Daily forecast error residuals per city |
| trade_attribution | Per-fill attribution: city, PnL, fees, slippage |
| calibration_diagnostics | Every market evaluated per cycle (edge, horizon, mode) |
| experiment_runs | One row per day×mode: trade counts, PnL, Brier summary |
| bot_config | Runtime key/value overrides (GUMBEL_MODE, BRIER_BLOCK_ENABLED) |

All queries use `CAST(col AS DATE)` not `DATE(col)` — Azure SQL syntax.
All pyodbc rows converted via `dict(zip([d[0] for d in cur.description], row))`.

## Key Entry Points
- `bot_runner.py` — Main container entry point (WebSocket + weather + trade loop)
- `src/index.py` — Trade cycle: scan → brain → risk → execute → log
- `analytics/calibration_report.py` — A/B/C experiment summary by mode
- `scripts/backtest.py` — Performance report: Sharpe, drawdown, Brier, PnL by city
- `scripts/check_outcomes.py` — Nightly: settle fills, write Brier scores, update PnL
- `scripts/set_gumbel_mode.py` — Manually override GUMBEL_MODE mid-experiment
- `tests/test_layout_math.py` — Math unit tests

## Current Status
| Component | Status |
|---|---|
| Kalshi REST + WebSocket | ✅ Complete |
| Weather ingestion (Open-Meteo) | ✅ Complete (persistent session, exponential backoff) |
| WebSocket reconnect backoff | ✅ Complete (exp. backoff 5→80s + jitter, 503 logging) |
| Azure SQL schema (16 tables) | ✅ Complete |
| EV engine + fee model | ✅ Complete |
| Particle filter (SMC) | ✅ Complete |
| 8-gate gating logic | ✅ Complete |
| LogitJumpDiffusion brain | ✅ Complete (wired, Gumbel-corrected) |
| AdaptiveBiasFilter | ✅ Complete (wired in weather_estimator.py) |
| Fill reconcile (post-submit) | ✅ Complete (get_portfolio_fills in trade_cycle) |
| Backtest framework | ✅ Complete (scripts/backtest.py) |
| Calibration report | ✅ Complete (analytics/calibration_report.py, Azure SQL) |
| City risk guard (Brier) | ✅ Complete (experiment-safe: BRIER_BLOCK_ENABLED flag) |
| Gumbel A/B/C experiment | ⏳ Phase 3 in progress — ends 2026-06-12 |
| trade_attribution populate | ⚠️ Table exists but rows not being written — backtest PnL by city unavailable |
| DEN/THOU YES bias | ⚠️ Model overestimates YES for Denver + Thousand Oaks (avg_edge +15c) |
| Kelly multiplier tuning | ⚠️ Held at floor (0.094) — pending post-experiment Brier recalibration |
| requirements.txt | ❌ Missing |

## Gumbel A/B/C Experiment
Phase 3 schedule (auto-switched at 4AM UTC by bot_runner.py):
- 2026-06-04 to 06-06: `half`
- 2026-06-07 to 06-09: `none` ← currently running
- 2026-06-10 to 06-12: `full`

**Do not change anything before June 12.**

## Post-Experiment Plan — June 13
Run these in order. All commands from the repo root.

### 1. Read the data
```bash
python analytics/calibration_report.py --days 9     # Phase 3 side-by-side
python scripts/backtest.py --days 30 --csv jun13.csv # Sharpe + drawdown + Brier + PnL
```

### 2. Pick winning Gumbel mode
Compare `none` vs `half` vs `full` on:
- Brier score (lower = better calibration)
- Total PnL (from positions table — `trade_attribution` is currently empty)
- Fill count and avg edge

Lock the winner in `src/config/experiment.py` or via:
```bash
python scripts/set_gumbel_mode.py <mode>
```

### 3. Enable per-city Brier blocking
Now that experiment data is complete, flip the flag:
```sql
UPDATE bot_config SET value='true', updated_at=GETDATE()
WHERE config_key='BRIER_BLOCK_ENABLED';
-- or INSERT if missing:
INSERT INTO bot_config (config_key, value, updated_at) VALUES ('BRIER_BLOCK_ENABLED','true',GETDATE());
```
Cities currently flagged as candidates (from backtest run 2026-06-07):
- LAX avg_brier=0.64, NYC avg_brier=0.62, TDC avg_brier=0.54 — will be throttled/blocked
- DEN avg_brier=0.22, THOU avg_brier=0.19 — currently OK

### 4. Fix DEN/THOU YES bias
Check if the winning mode eliminates or reduces the bias:
```bash
python analytics/calibration_report.py --days 30  # look at city-level edge column
```
If avg_edge > +0.05 still shows for DEN/THOU → add a per-city fixed offset dict in
`src/brain/weather_estimator.py::estimate_p_yes()` alongside `_adaptive_bias.correction()`.

### 5. Fix trade_attribution not being populated
The `trade_attribution` table exists but `log_execution_fill` in `src/logging/trade_logger.py`
is not writing to it. Without this, `scripts/backtest.py --mode half` won't show per-city PnL.
Check `trade_logger.py::log_execution_fill()` and wire the INSERT to `trade_attribution`.

### 6. Recalibrate Kelly multiplier
Once 30+ settled trades exist under the winning mode, check rolling Brier:
- If Brier < 0.20 → bump `kelly_multiplier` in `src/index.py` from 0.25 → 0.35
- If Brier still ≥ 0.25 → investigate prediction calibration before changing Kelly

### 7. Fix backtest Brier-by-mode join inflation
`scripts/backtest.py::_section_brier_by_mode` joins predictions→orders on ticker+date,
which over-counts when multiple orders share a ticker on the same day (currently shows
19,127 rows for `half` mode — likely inflated). Fix by using a subquery that picks
one gumbel_mode per (ticker, trade_date) before aggregating.

## Development Rules
- All I/O must be `async/await` — this is a fully async codebase
- Type hints required on all public methods
- Never commit credentials (`Credentials/` is gitignored)
- `ENV_MODE=paper` by default — never set `live` without explicit authorization
- Math functions must stay pure (no I/O, no DB calls)
- New math needs a unit test before merging
- Layers import downward only: Layer 4 → 3 → 2 → 1 → 0 (no circular)

## Agents Available
Use these specialized agents via `Agent` tool or reference them in prompts:
- `.claude/agents/kalshi-connector.md` — Kalshi API + WebSocket expert
- `.claude/agents/weather-connector.md` — Weather API + forecast mapping expert
- `.claude/agents/data-architect.md` — SQLite schema + query expert
- `.claude/agents/math-engine.md` — Arbitrage, EV, Kelly, particle filter expert
- `.claude/agents/ml-brain.md` — ML probability model + brain protocol expert
- `.claude/agents/execution-manager.md` — Order execution + position lifecycle expert
- `.claude/agents/risk-manager.md` — Risk controls + capital limits expert
- `.claude/agents/backtest-engineer.md` — Backtest framework + metrics expert

## Custom Commands
- `/project:analyze-market` — Scan for arbitrage opportunities
- `/project:check-positions` — View open positions + P&L
- `/project:run-paper` — Start paper trading session
- `/project:backtest` — Run strategy backtest
- `/project:health-check` — Verify all system components
- `/project:deploy` — Deploy via Docker
