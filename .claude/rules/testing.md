# Testing Rules

## Math First
- All new math functions need a unit test in `tests/test_layout_math.py` before merging
- Math tests run in isolation: no DB, no API, no network
- Cover: normal case, edge cases (prices at 0 and 100), boundary conditions
- Fee formula test must match official Kalshi documentation exactly

## Test Structure
- Framework: `pytest`
- Clear test names: `test_ev_engine_negative_for_overpriced_market` (not `test_1`)
- File per layer: `test_math.py`, `test_db.py`, `test_ingest.py`, `test_brain.py`
- DB tests: in-memory SQLite — `conn = sqlite3.connect(":memory:")`

## What Not to Mock
- Never mock the math layer (`DecisionEngine`, `EVEngine`, `TradeGating`)
- Never mock SQLite — use in-memory DB
- Mock: Kalshi API calls, Open-Meteo API calls, WebSocket connections

## Required Coverage
| Component | Required Tests |
|---|---|
| Fee formula | Matches official Kalshi formula for 5+ price points |
| EV calculation | Accounts for fees in buy and settle scenarios |
| 8-gate logic | Each gate has at least one pass test + one fail test |
| Kelly criterion | Caps at max_kelly, returns 0 for negative EV |
| Brain model | Calibration check: Brier score < 0.25 on test set |

## Backtest Before Deploy
- Any strategy or math change: run backtest before deploying
- Minimum: 14 days of historical data, report Sharpe + max drawdown
- Compare: before vs after the change
