# /project:backtest

Run a backtest over historical data in DWTrader.db to evaluate strategy performance.

## Steps
1. Run `check_data.py` to discover available date range and data density
2. Check if `src/backtest/` directory exists — if not, ask the Backtest Engineer agent to build it first
3. Verify `scripts/backtest.py` entry point exists
4. Determine date range: default = last 30 days of `orderbook_events`
5. Run backtest with specified range
6. Report evaluation metrics

## Pre-backtest Checks
- `orderbook_events` table has at least 7 days of data (minimum meaningful period)
- `weather_data` table has forecasts covering the backtest period
- `data/DWTrader.db` is not locked (check for stale `.db-wal` journal > 10MB)

## Run Command
```bash
python scripts/backtest.py \
  --start 2024-12-01 \
  --end 2025-01-15 \
  --mode rule-only  # or: brain, layer2
```

## Output Format
```
=== BACKTEST RESULTS ===
Mode: rule-only  Period: 2024-12-01 → 2025-01-15 (45 days)
Markets scanned: 1,240  Signals generated: 89  Trades placed: 31

Performance:
  Win rate:      58.1%  (18/31 profitable)
  Avg EV:        +2.3¢/contract
  Gross PnL:     +$34.20
  Fees paid:     -$6.10
  Net PnL:       +$28.10

Risk:
  Sharpe ratio:  1.42
  Max drawdown:  -$12.50  (on 2024-12-18)
  Daily loss hit: 0 times

Calibration (if brain used):
  Brier score:  0.18
  Log-loss:     0.41

Comparison vs baseline (rule-only):
  Net PnL:  rule-only $28.10 | brain $34.50 (+23%)
```

## Notes
- Backtest results saved to `data/backtest_{timestamp}.db` (never overwrites live DB)
- Minimum 14 days of data for results to be statistically meaningful
- Run rule-only first (baseline), then brain mode to measure improvement
- Any Sharpe < 1.0 or max drawdown > 50% of bankroll → review before deploying strategy
