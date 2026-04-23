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

## Database — SQLite (`data/DWTrader.db`, 4.7MB)
| Table | Purpose |
|---|---|
| scans | Market observations (price, spread, volume) |
| decision_log | Math engine output per scan |
| intents | Pre-execution trade intentions |
| orders | Submitted orders |
| executions | Order fills (UNIQUE on order_id + trade_id) |
| positions | Active positions with running PnL |
| position_events | Audit log for all position changes |
| weather_data | Historical + forecast weather signals |
| orderbook_events | Raw orderbook snapshots/deltas (JSON, for replay) |

WAL mode, foreign keys enabled, busy_timeout=10s.

## Key Entry Points
- `ingest_app.py` — Layer 1 only (always on, no trading)
- `src/index.py` — Full trading loop: scan → decide → risk → execute → log
- `check_data.py` — Inspect DB contents
- `scripts/backtest.py` — Run backtest over historical data (to build)
- `tests/test_layout_math.py` — Math unit tests

## Current Status
| Component | Status |
|---|---|
| Kalshi REST + WebSocket | ✅ Complete |
| Weather ingestion (Open-Meteo) | ✅ Complete |
| Database schema (9 tables) | ✅ Complete |
| EV engine + fee model | ✅ Complete |
| Particle filter (SMC) | ✅ Complete |
| 8-gate gating logic | ✅ Complete |
| Decision engine (rules-based) | ⚠️ Hardcoded prob_win=0.98 |
| Risk checks | ⚠️ Daily limits not wired to DB |
| Fill polling after submit | ❌ Missing |
| ML brain (BrainModel protocol) | ❌ Missing |
| Backtest framework | ❌ Missing |
| requirements.txt | ❌ Missing |

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
