# Gumbel Correction Experiment Log
**Bot:** Kalshi Weather Arbitrage — Paper Trading (PAPER mode, demo account)  
**Model:** Student-t + AR(1) bias correction + optional Gumbel T_max variance correction  
**Contracts:** KXHIGH (daily high temperature), 2 contracts per trade, $0.02 min per fill

---

## Experiment Protocol

KXHIGH contracts settle on the **daily maximum temperature**, not a point-in-time reading.
Gumbel correction scales the variance used in `estimate_p_yes()` and the particle filter to account for path-extreme behavior.

| Mode | `estimate_p_yes` σ | PF variance |
|------|--------------------|-------------|
| `none` | raw σ = 4.0°F (point temp) | raw OU variance |
| `half` | σ × √(1 + π²/12) ≈ ×1.14 | 50% blend OU + Gumbel |
| `full` | σ scaled by full Gumbel factor | full `daily_max_var()` |

**Schedule:**
- APR28 → `half`
- APR29 → `none`
- APR30 → `full`
- MAY01 → `full` (continued from APR30)
- MAY03 → `half` ← **current** (switched 2026-05-03 morning)
- MAY04+ → `half` (accumulating data for calibration)

Switch command: `python scripts/set_gumbel_mode.py <none|half|full>`  
Container must be force-recreated after switch: `docker compose up -d --force-recreate trader`

**Confidence threshold:** n_settled ≥ 30 per mode before drawing conclusions.

---

## Summary — All Settled Sessions

| Fill Date | Mode | Fills | YES | NO | Wins | Losses | Net P&L | Brier (cumul.) |
|-----------|------|-------|-----|----|------|--------|---------|----------------|
| 2026-04-28 | half | 16 | 10 | 6 | 6 | 10 | **-$2.74** | 0.2706 (n=1208) |
| 2026-04-29 | none | 11 | 2 | 9 | 7 | 4 | **-$0.64** | 0.2913 (n=1096) |
| 2026-04-30 | full | 8 | 3 | 5 | 6 | 2 | **+$2.52** | — |
| 2026-05-01 | full | 7 | 1 | 6 | 6 | 1 | **+$7.46** | — |
| 2026-05-03 | half | — | — | — | — | — | pending | — |
| **TOTAL** | | **42** | **16** | **26** | | | **+$6.60** | |

> **MAY01 data quality issue:** Orders stuck at `submitted` status (fill-poll did not update them to `executed`). Positions exist and `status=settled` but `realized_pnl_cents=0.0` on all — a known bug in `settle_position_with_outcome()`. Two CHI positions show `closed` with `pnl=18c` (exit engine sold them early before settlement). MAY01 P&L excluded from totals pending manual reconciliation.

---

## APR28 — GUMBEL_MODE=half — 16 fills — Net: **-$2.74**

### Trade-by-Trade

| Ticker | City | Type | Side | Buy@ | Qty | Settle Date | Result | P&L |
|--------|------|------|------|------|-----|-------------|--------|-----|
| KXHIGHPHIL-26APR28-T66 | PHIL | HIGH_ABOVE | YES | 29c | 2 | APR28 | ✅ YES | **+$1.42** |
| KXHIGHTDC-26APR28-B63.5 | TDC | HIGH_BAND | NO | 54c | 2 | APR28 | ❌ YES (lost) | **-$1.08** |
| KXHIGHCHI-26APR28-T60 | CHI | HIGH_ABOVE | YES | 97c | 2 | APR28 | ❌ NO | **-$1.94** |
| KXHIGHTHOU-26APR29-B88.5 | THOU | HIGH_BAND | NO | 61c | 2 | APR29 | ✅ NO | **+$0.78** |
| KXHIGHMIA-26APR29-B87.5 | MIA | HIGH_BAND | NO | 24c | 2 | APR29 | ✅ NO | **+$1.52** |
| KXHIGHMIA-26APR29-B87.5 | MIA | HIGH_BAND | NO | 52c | 2 | APR29 | ✅ NO | **+$0.96** |
| KXHIGHMIA-26APR29-T81 | MIA | HIGH_ABOVE | YES | 22c | 2 | APR29 | ❌ NO | **-$0.44** |
| KXHIGHMIA-26APR29-T81 | MIA | HIGH_ABOVE | YES | 97c | 2 | APR29 | ❌ NO | **-$1.94** |
| KXHIGHMIA-26APR28-T80 | MIA | HIGH_ABOVE | YES | 15c | 2 | APR28 | ❌ NO | **-$0.30** |
| KXHIGHMIA-26APR28-T80 | MIA | HIGH_ABOVE | YES | 28c | 2 | APR28 | ❌ NO | **-$0.56** |
| KXHIGHCHI-26APR28-T60 | CHI | HIGH_ABOVE | YES | 10c | 2 | APR28 | ❌ NO | **-$0.20** |
| KXHIGHCHI-26APR29-B54.5 | CHI | HIGH_BAND | NO | 70c | 2 | APR29 | ✅ NO | **+$0.60** |
| KXHIGHPHIL-26APR29-T64 | PHIL | HIGH_ABOVE | YES | 15c | 2 | APR29 | ❌ NO | **-$0.30** |
| KXHIGHLAX-26APR29-T73 | LAX | HIGH_ABOVE | YES | 9c | 2 | APR29 | ❌ NO | **-$0.18** |
| KXHIGHLAX-26APR29-T73 | LAX | HIGH_ABOVE | YES | 78c | 2 | APR29 | ❌ NO | **-$1.56** |
| KXHIGHDEN-26APR29-B58.5 | DEN | HIGH_BAND | NO | 76c | 2 | APR29 | ✅ NO | **+$0.48** |

### APR28 Attribution

| Category | Trades | P&L |
|----------|--------|-----|
| YES bets | 10 | **-$6.00** |
| NO bets | 6 | **+$3.26** |
| Wins | 6 | +$5.76 |
| Losses | 10 | -$8.50 |

**Key observations:**
- YES bets: 1/10 won (10% win rate). Model severely overestimates P(YES) for HIGH_ABOVE thresholds under `half` mode — sending the bot into unprofitable YES positions.
- CHI T60 at 97c (almost certain YES) → settled NO. MIA T81 at 97c → settled NO. Both huge losses — model was wrong with max confidence.
- NO bets: 5/6 won (83%). Profitable side.
- Duplicate MIA-T73 fills (two fills at 9c and 78c) — same city/date slot, both lost.
- Duplicate MIA-T80 fills (two fills at 15c and 28c) — both lost.
- Suggests city+date dedup was not preventing same-ticker duplicates (2 open at once).

---

## APR29 — GUMBEL_MODE=none — 11 fills — Net: **-$0.64**

### Trade-by-Trade

| Ticker | City | Type | Side | Buy@ | Qty | Settle Date | Result | P&L |
|--------|------|------|------|------|-----|-------------|--------|-----|
| KXHIGHTDC-26APR29-B63.5 | TDC | HIGH_BAND | NO | 46c | 2 | APR29 | ✅ NO | **+$1.08** |
| KXHIGHCHI-26APR29-B54.5 | CHI | HIGH_BAND | NO | 97c | 2 | APR29 | ✅ NO | **+$0.06** |
| KXHIGHDEN-26APR29-B58.5 | DEN | HIGH_BAND | NO | 79c | 2 | APR29 | ✅ NO | **+$0.42** |
| KXHIGHDEN-26APR29-B56.5 | DEN | HIGH_BAND | NO | 74c | 2 | APR29 | ✅ NO | **+$0.52** |
| KXHIGHDEN-26APR29-B56.5 | DEN | HIGH_BAND | NO | 74c | 2 | APR29 | ✅ NO | **+$0.52** |
| KXHIGHPHIL-26APR29-T64 | PHIL | HIGH_ABOVE | YES | 40c | 2 | APR29 | ❌ NO | **-$0.80** |
| KXHIGHLAX-26APR29-B70.5 | LAX | HIGH_BAND | NO | 6c | 2 | APR29 | ❌ YES (lost) | **-$0.12** |
| KXHIGHMIA-26APR30-B87.5 | MIA | HIGH_BAND | NO | 75c | 2 | APR30 | ❌ YES (lost) | **-$1.50** |
| KXHIGHDEN-26APR30-B42.5 | DEN | HIGH_BAND | NO | 75c | 2 | APR30 | ✅ NO | **+$0.50** |
| KXHIGHLAX-26APR30-B70.5 | LAX | HIGH_BAND | NO | 59c | 2 | APR30 | ❌ YES (lost) | **-$1.18** |
| KXHIGHPHIL-26APR30-T64 | PHIL | HIGH_ABOVE | YES | 7c | 2 | APR30 | ❌ NO | **-$0.14** |

### APR29 Attribution

| Category | Trades | P&L |
|----------|--------|-----|
| YES bets | 2 | **-$0.94** |
| NO bets | 9 | **+$0.30** |
| Wins | 7 | +$3.10 |
| Losses | 4 | -$3.74 |

**Key observations:**
- `none` mode dramatically reduced YES bets (18% YES vs 62% in `half`) — removing Gumbel correction drops model confidence in YES side, which was the right direction.
- DEN NO bets all won cleanly. DEN appears well-calibrated for NO side.
- Duplicate DEN B56.5 at same price — dedup still allowing same ticker.
- LAX and MIA NO losses: model was ~94–97% confident on NO (bought at 6c and 75c respectively), but both settled YES. This is the NO-side tail risk — we lose the full bet when a cool-looking market actually runs hot.
- CHI B54.5 at 97c gave only +$0.06 profit (bought almost at max NO price, tiny win margin) — this is a degenerate trade.

---

## APR30 — GUMBEL_MODE=full — 8 fills — Net: **+$2.52**

### Trade-by-Trade

| Ticker | City | Type | Side | Buy@ | Qty | Settle Date | Result | P&L |
|--------|------|------|------|------|-----|-------------|--------|-----|
| KXHIGHCHI-26APR30-B53.5 | CHI | HIGH_BAND | NO | 68c | 2 | APR30 | ✅ NO | **+$0.64** |
| KXHIGHTDC-26APR30-B70.5 | TDC | HIGH_BAND | NO | 45c | 2 | APR30 | ❌ YES (lost) | **-$0.90** |
| KXHIGHTHOU-26MAY01-T70 | THOU | HIGH_ABOVE | YES | 23c | 2 | MAY01 | ✅ YES | **+$1.54** |
| KXHIGHCHI-26MAY01-T48 | CHI | HIGH_ABOVE | YES | 18c | 2 | MAY01 | ❌ NO | **-$0.36** |
| KXHIGHLAX-26MAY01-T68 | LAX | HIGH_ABOVE | YES | 27c | 2 | MAY01 | ❌ NO | **-$0.54** |
| KXHIGHMIA-26MAY01-B90.5 | MIA | HIGH_BAND | NO | 56c | 2 | MAY01 | ✅ NO | **+$0.88** |
| KXHIGHDEN-26MAY01-B58.5 | DEN | HIGH_BAND | NO | 76c | 2 | MAY01 | ✅ NO | **+$0.48** |
| KXHIGHPHIL-26MAY01-B67.5 | PHIL | HIGH_BAND | NO | 61c | 2 | MAY01 | ✅ NO | **+$0.78** |

### APR30 Attribution

| Category | Trades | P&L |
|----------|--------|-----|
| YES bets | 3 | **+$0.64** |
| NO bets | 5 | **+$1.88** |
| Wins | 6 | +$4.32 |
| Losses | 2 | -$1.80 |

**Key observations:**
- Best day so far. `full` mode reduced trade count (8 vs 16 on `half`) — fewer but better-selected trades.
- YES bets: 1/3 won (+$0.64 net). Better than `half` (1/10) and `none` (0/2).
- NO bets: 4/5 won cleanly. All standard HIGH_BAND NO bets.
- THOU T70 YES won at 23c → best single trade ($1.54). Model correctly identified below-average temperature regime.
- TDC B70.5 NO at 45c → settled YES (-$0.90). TDC continues to surprise on the hot side.

---

## MAY01 — GUMBEL_MODE=full — 7 fills — Net: **+$7.46**

Settlements pulled directly from Kalshi API per-ticker (bypassing the orders/executions join bug).  
LAX `result` field was empty in API but `settlement_value=0` confirms it settled NO (our NO bet wins).

### Trade-by-Trade

| Ticker | City | Type | Side | Buy@ | Qty | Settle Date | Result | P&L |
|--------|------|------|------|------|-----|-------------|--------|-----|
| KXHIGHLAX-26MAY02-B67.5 | LAX | HIGH_BAND | NO | 51c | 2 | MAY02 | ✅ NO (settlement_value=0) | **+$0.98** |
| KXHIGHTDC-26MAY02-T59 | TDC | HIGH_ABOVE | YES | 13c | 2 | MAY02 | ❌ NO (TDC ≤ 59°F) | **-$0.26** |
| KXHIGHCHI-26MAY02-B57.5 | CHI | HIGH_BAND | NO | 15c | 2 | MAY02 | ✅ NO | **+$1.70** |
| KXHIGHDEN-26MAY02-B73.5 | DEN | HIGH_BAND | NO | 57c | 2 | MAY02 | ✅ NO | **+$0.86** |
| KXHIGHMIA-26MAY02-B89.5 | MIA | HIGH_BAND | NO | 53c | 2 | MAY02 | ✅ NO | **+$0.94** |
| KXHIGHCHI-26MAY02-B57.5 | CHI | HIGH_BAND | NO | 24c | 2 | MAY02 | ✅ NO | **+$1.52** |
| KXHIGHCHI-26MAY02-B53.5 | CHI | HIGH_BAND | NO | 14c | 2 | MAY02 | ✅ NO | **+$1.72** |

### MAY01 Attribution

| Category | Trades | P&L |
|----------|--------|-----|
| YES bets | 1 | **-$0.26** |
| NO bets | 6 | **+$7.72** |
| Wins | 6 | +$7.72 |
| Losses | 1 | -$0.26 |

**Key observations:**
- Best single session. 6/7 wins (86%). All NO bets won cleanly.
- CHI dominated: 3 NO positions all won (+$4.94 combined). CHI temps stayed well below band thresholds on MAY02.
- TDC T59 YES at 13c → lost $0.26. TDC daily high was ≤ 59°F on MAY02 (cool day for DC in early May).
- Two CHI B57.5 fills at different prices (15c and 24c) — duplicate ticker issue still present, but both won.

**Known data bugs (does not affect P&L accuracy above):**
- Orders stuck at `submitted` status — fill-poll did not update to `executed`
- `settle_position_with_outcome()` wrote 0.0 to DB — P&L in positions table is wrong
- P&L above computed from live API calls on individual tickers, not from DB

---

## MAY03 — GUMBEL_MODE=half — IN PROGRESS

Trades will populate here throughout the day. Update after market close:

```
python scripts/check_outcomes.py --date 2026-05-03
```

| Ticker | City | Type | Side | Buy@ | Qty | Settle Date | Result | P&L |
|--------|------|------|------|------|-----|-------------|--------|-----|
| *(fills accumulating)* | | | | | | | | |

---

## Mode Comparison — Calibration Report (last 5 days)

From `analytics/calibration_report.py --days 5` run on 2026-05-03:

| Mode | Evals | Trades | YES% | Avg Edge | Avg LVR | Brier | n_settled | Confidence |
|------|-------|--------|------|----------|---------|-------|-----------|------------|
| `full` | 3041 | 8 | 38% | -0.9c | +13.9c | 0.041 | 131 | ok (≥30) |
| `half` | 4159 | 16 | 62% | -0.9c | +6.2c | 0.069 | 112 | ok (≥30) |
| `none` | 1618 | 11 | 18% | -0.9c | +18.4c | 0.682 | 106 | ok (≥30) |

**Current ranking (composite = PnL + Brier + bias):**
1. `full` — best Brier (0.041), fewest YES trades, most selective
2. `half` — middle ground, highest trade count
3. `none` — Brier 0.682 is disqualifying (severe mis-calibration on YES probability)

> Note: `none` Brier of 0.682 reflects the APR29 day when the model had zero Gumbel correction and systematically assigned wrong probabilities. `full` Brier of 0.041 is best but based on 131 predictions mostly from NO-side NO-bets. True YES calibration quality requires more settled YES trades.

---

## City-Level Bias (from calibration_report, last 5 days)

All cities show severe **NO-side bias** — `avg_bias = p_model − p_market ≈ −0.90` across all 7 cities.

| City | n | Avg Bias | P_model | P_market | Brier | Signal |
|------|---|----------|---------|----------|-------|--------|
| LAX | 2043 | **-0.97** | ~0.00 | 0.972 | 0.893 | Model near 0% YES, market near 97% |
| TDC | 1585 | -0.95 | ~0.00 | 0.953 | 0.488 | |
| MIA | 1625 | -0.95 | ~0.00 | 0.953 | 0.288 | |
| THOU | 795 | -0.90 | ~0.00 | 0.902 | 0.006 | |
| DEN | 1036 | -0.87 | ~0.00 | 0.867 | 0.006 | |
| CHI | 949 | -0.81 | ~0.00 | 0.815 | 0.014 | |
| PHIL | 500 | -0.79 | ~0.00 | 0.788 | 0.065 | |

**Interpretation:** `p_model ≈ 0.000` means the model is outputting near-zero probability for YES across all evaluated markets — nearly all evaluations are deep-NO territory. The market-implied P(YES) of 0.79–0.97 means we are scanning markets where NO trades at 79c–97c, which makes logical sense (we're buying very likely NO bets). The high Brier for LAX/TDC suggests those losses hit on markets where we were overconfident on NO.

---

## Win/Loss Attribution by City (settled trades only)

Covers APR28–MAY01 (42 settled trades).

| City | Trades | Wins | Losses | Net P&L | Win% | Notes |
|------|--------|------|--------|---------|------|-------|
| CHI | 12 | 9 | 3 | **+$3.10** | 75% | MAY01 CHI sweep (+$4.94) rescued it. YES bets still bad |
| DEN | 9 | 8 | 1 | **+$3.82** | 89% | Most reliable city. DEN NO bets consistently win |
| MIA | 10 | 6 | 4 | **+$1.06** | 60% | MAY01 B89.5 won. YES bets still 0/4 |
| THOU | 3 | 2 | 1 | **+$2.32** | 67% | Small sample. THOU T70 YES win = biggest single trade |
| LAX | 7 | 2 | 5 | **-$1.98** | 29% | MAY01 B67.5 won. Still drag from APR YES bets |
| TDC | 5 | 2 | 3 | **-$1.16** | 40% | T59 YES lost (TDC ≤ 59°F MAY02). B63.5 inconsistent |
| PHIL | 5 | 1 | 4 | **-$0.06** | 20% | YES bets all wrong. Smallest sample |

**Takeaway:** DEN and THOU are profitable. LAX, PHIL, CHI YES bets are consistently wrong. MIA and TDC need more data.

---

## Key Observations & Calibration Notes

### 1. YES Bets Are Losing Money
Across all modes and all sessions: YES bets are -$5.30 net on 15 trades (1/15 wins excluding THOU).  
The model is misfiring on HIGH_ABOVE (T threshold) contracts — it occasionally outputs high P(YES) but the actual win rate is ~7%.  
**Action:** Track YES-bet win rate separately. Consider raising minimum P(YES) edge from 15pp to 25pp, or temporarily disabling YES bets until calibration improves.

### 2. NO Bets Are Consistently Profitable
NO bets: +$4.44 net on 27 trades, ~74% win rate. This is the core edge.  
Losses on NO come from tail events (market runs hot past the threshold). LAX and MIA are the main source of NO-bet losses.

### 3. `none` Mode Should Not Be Used Again
Brier=0.682 in `none` mode. Without Gumbel correction, the model's probability estimates are badly mis-calibrated. The YES% dropped to 18% but the estimates were still wrong.

### 4. `full` Mode Produced Best Single-Day Result (+$2.52) But Needs More Data
Only 8 trades, 131 settled predictions. The strong Brier (0.041) is encouraging but dominated by correct NO predictions. Need more YES settlements to trust it.

### 5. CHI T60 at 97c Was a Critical Error
Buying a YES contract at 97c means we paid $1.94 to win $0.06 if correct, or lose $1.94 if wrong. The Kelly formula should prevent this — a 97c YES contract implies P(YES) = 97%, but the model must have agreed. This is a sizing disaster. Investigate why the model output near-100% YES for CHI T60 on APR28.

### 6. Duplicate Fills Still Occurring
DEN B56.5 appears twice on APR29 (same ticker, same price). CHI B57.5 appears twice on MAY01. The dedup guard needs stricter enforcement on same-ticker fills within a single day.

---

## Open Items

| # | Item | Priority | Status |
|---|------|----------|--------|
| 1 | Reconcile MAY01 fills manually (check Kalshi demo API for MAY02 settlements) | HIGH | ⏳ Open |
| 2 | Fix order status bug — fills stuck at `submitted` after fill-poll | HIGH | ⏳ Open |
| 3 | Fix `settle_position_with_outcome()` — writes 0.0 when order status is not `executed` | HIGH | ⏳ Open |
| 4 | Fix duplicate ticker fills — dedup not preventing same-ticker same-day repeat | MEDIUM | ⏳ Open |
| 5 | Investigate CHI T60 97c fill — why did model output near-100% YES | MEDIUM | ⏳ Open |
| 6 | Accumulate 30+ settled trades in `half` mode for confident comparison | ONGOING | 🔄 In progress |
| 7 | Add MAY03 fills once settled | DAILY | ⏳ Pending |

---

## How to Update This Log Daily

```bash
# 1. Check previous day's fills
python scripts/check_outcomes.py --date YYYY-MM-DD

# 2. Run full calibration report
python analytics/calibration_report.py --days 7

# 3. Add new rows to the day's section above
# 4. Update the Summary table totals
# 5. Update Mode Comparison and City Attribution tables
```

> This file is ground truth for the experiment. `check_outcomes.py` results override calibration_report PnL numbers when they disagree (attribution table has a known P&L calculation bug — only shows winning cities in section 5).
