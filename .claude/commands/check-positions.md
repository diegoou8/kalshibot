# /project:check-positions

View open positions, recent fills, today's P&L, and risk limit status from DWTrader.db.

## Steps
1. Connect to `data/DWTrader.db` (read-only)
2. Query `positions` table for rows where qty > 0
3. Query `executions` for fills in the last 24 hours
4. Query `orders` for today's submitted orders
5. Query `scans` for the most recent price per open ticker (for unrealized PnL)
6. Query `decision_log` for today's decisions (passed + rejected)
7. Calculate unrealized PnL: `(current_price - avg_price) * qty` per position

## Output Format
```
=== OPEN POSITIONS — {timestamp} ===
Ticker                Side  Qty  Avg Price  Cost    Mkt Price  Unreal PnL
HIGHNY-2025-01-15-T65  YES   10   45¢       $4.50    48¢       +$0.30
HIGHDFW-2025-01-15-T72 YES    5   82¢       $4.10    80¢       -$0.10

Total Cost Basis: $8.60  Total Unrealized: +$0.20

=== TODAY'S ACTIVITY ===
Scans run: 150  Signals found: 12  Trades placed: 3  Rejected: 9
Volume: $12.60  Realized PnL: +$1.40  Unrealized: +$0.20
Net today: +$1.60

=== RISK STATUS ===
Daily Volume:      $12.60  /  $5,000  limit  (0.3%)
Daily Loss:        $0.00   /  $250    limit   (OK)
Open Positions:    2
Halt Flag:         NOT SET
ENV_MODE:          paper
```

## Notes
- Pull market prices from most recent `scans` row per ticker
- Flag positions where unrealized loss > $5 (potential close candidates)
- Flag if halt.flag exists in data/
- If no positions open, say so clearly
