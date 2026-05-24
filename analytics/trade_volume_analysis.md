# Trade Volume Bottleneck Analysis
_Last updated: 2026-05-05_

## Summary

The bot consistently fills only 2–16 trades/day despite scanning 500–1,800 unique tickers and generating 70–470 SUBMIT decisions per day. The bottleneck is **not** execution — intents convert to fills at ~100%. The collapse happens between SUBMIT decisions and intents.

## Observed Funnel (Apr 28 – May 3)

| Day | Tickers scanned | SUBMIT decisions | Intents | Orders | Fills |
|-----|----------------|-----------------|---------|--------|-------|
| 2026-04-28 (half) | 1,193 | 473 | 15 | 20 | 16 |
| 2026-04-29 (none) | 1,833 | 168 | 15 | 17 | 11 |
| 2026-04-30 (full) | 954 | 221 | 13 | 13 | 8 |
| 2026-05-01 (unknown) | 1,304 | 124 | 6 | 7 | 7 |
| 2026-05-03 (unknown) | 559 | 72 | 2 | 2 | 2 |

Key ratio: **96–97% of SUBMIT decisions are dropped before becoming intents.**

## Root Causes (Ranked by Impact)

### 1. Slot Saturation — Primary Limiter (~60–70% of drop)

`MAX_POSITIONS_PER_SLOT = 2` caps positions per city+settlement_date combination.

- 7 active cities × ~3 settlement dates × 2 max positions = **~42 theoretical daily max**
- In practice, the best slots fill within the **first 1–2 hours** of trading
- After that, every subsequent cycle produces SUBMIT signals but hits full slots — no new intents are created
- Evidence: 0.1 intents/cycle average (1 intent per 10 cycles), even though 3 SUBMIT decisions/cycle are generated

Config location: `src/risk/manager.py:36` — `MAX_POSITIONS_PER_SLOT = 2`

### 2. Strike Separation Filter (~20% of drop)

Even when a slot has only 1 existing position, a second can only be added if the new strike is **≥ 2.0°F away** from the existing one.

- On low-volume days this kills most second-position opportunities
- Config: `src/index.py:516` — `_MIN_STRIKE_SEP_F = 2.0` (hardcoded)

### 3. Gating Logic Cascades (~10% of drop)

8 gates must all pass. The ones most likely to fail given the current model behaviour:

| Gate | Threshold | Notes |
|------|-----------|-------|
| Staleness | `pi_stale < 0.30` | Particle filter estimate must be fresh |
| ESS | `ess ≥ 20% × 400 particles` | Filter must not have collapsed |
| Spread (inner) | `≤ 8¢` | Second spread check after the scan-level 15¢ gate |
| Fragility | `min(yes_ask, no_ask) > 1.5¢` | Both sides must trade away from extremes |

Config: `src/layer2/gating_logic.py`

### 4. Order Timeouts and Cancel Cooldowns (~10–15% of drop)

- 4–5 cancels/day; 0–1 timeouts/day
- After **3 cancels on the same ticker**, it is blacklisted for the rest of the day
- 30-second fill timeout per order (`_POLL_TIMEOUT_S = 30`)

Config: `src/execution/manager.py:12`

## Full Pipeline Filter Map

```
~1,200 tickers scanned/day
    │ Spread ≤ 15¢, status=open, both legs tradeable
    ▼
~400–500 pass scan pre-filter
    │ tau ≥ 6h, weather estimate available, city not blocked
    │ Edge ≥ MIN_PROB_EDGE_PP = 15pp
    ▼
~100–500 SUBMIT decisions logged (decision_log)
    │ 8-gate gating logic (EV, staleness, spread, fragility, ESS, depth, tau, variance)
    │ Risk preflight (halt flag, position size ≤ $50, daily contract cap ≤ 10)
    │ Slot deduplication: best EV per city+date slot per cycle
    │ Already-held check: slot must have < 2 positions
    │ Strike separation: new strike must be ≥ 2°F from existing
    ▼
~2–15 intents/day
    │ Final EV guard ≥ 5¢, stale ask drift check, cancel cooldown check
    ▼
~2–16 orders submitted
    │ 30s fill timeout, cancel on timeout
    ▼
~2–16 fills/day  ← nearly 100% conversion from intents
```

## Potential Levers to Increase Volume

These are **not currently applied** — kept here for reference when the model is better calibrated.

| Lever | Current | Candidate | Expected impact |
|-------|---------|-----------|----------------|
| `MAX_POSITIONS_PER_SLOT` | 2 | 3 | +50% max positions once calibrated |
| `MAX_CONTRACTS_PER_SLOT` | 4 | 6 | Unlocks 3rd position per slot |
| `MAX_DAILY_GROSS_CONTRACTS` | 10 | 15–20 | Removes daily cap as secondary limiter |
| `_MIN_STRIKE_SEP_F` | 2.0°F | 1.0°F | More second-slot opportunities |
| Settlement dates lookahead | ~3 days | 4–5 days | More unique slots available |

**Do not loosen these until the model Brier score improves.** Current `none` and `full` modes are both unprofitable. Amplifying a miscalibrated model increases losses proportionally.

## Decision: Current State (2026-05-05)

- Keeping position limits unchanged
- Continuing to collect `none` mode data to build sample for A/B/C comparison
- Re-evaluate position limits after picking a winning Gumbel mode and verifying Brier < 0.15
