# Kalshi Weather Bot — Progress Log

## Session: 2026-04-21 (evening — continued)

### Outcome Check (yesterday's trades)
9/12 settled, 3 still pending (APR21 markets — today).

| Result | Count | Net |
|--------|-------|-----|
| Wins | 4 | +$1.76 |
| Losses | 5 | -$5.86 |
| **Total settled** | | **-$4.10** |

**Key insight**: ALL three YES-side bets lost (LAX-T71, MIA-T79, CHI-T73 pending). Most losses were from the brain being overconfident on forecasts that missed by ~5-6°F (LAX especially). σ=2.5°F was too tight.

### Notebook Audit (2026-04-21 evening)
Submitted full math stack to "Quant Desk Simulation" notebook for enterprise-grade review. Key findings:

| Finding | Verdict |
|---------|---------|
| Itô RN drift `0.5·σ²·tanh(x/2)·τ/24` | ✅ Mathematically exact |
| σ=4.0°F fixed | ❌ Must be MLE-fitted per city + horizon bin |
| σ_b=0.3 fixed | ❌ Must be Kalman-filtered from market trades |
| Kelly cap 0.15, multiplier 1.0 | ❌ 0.25× fractional Kelly (multiplier=0.25) required |
| Kelly net_odds ignores fees | ❌ `b = (100−price−fee)/(price+fee)` is correct formula |
| Gaussian temperature tails | ❌ Heavy tails (Student-t ν=7) required for extremes |
| IID forecast errors | ❌ AR(1) serial correlation; fix: `f_adj = f_raw + φ·e_{t-1}` |
| Market weight κ=0.3 fixed | ❌ Volume-gated softmax; high-volume = more informed traders |

### What We Shipped After the Audit

All 5 fixes implemented and tested (15/15 tests passing):

1. **Kelly multiplier 1.0 → 0.25** (`index.py`) — 0.25× fractional Kelly reduces ruin risk when σ is miscalibrated
2. **Fee-adjusted Kelly odds** (`engine.py`) — `net_odds = (100−price−fee)/(price+fee)` instead of `(100−price)/price`
3. **Volume-gated market weight** (`logit_jd.py`) — `vol_factor = log(1+v/100)/log(11)` boosts α_mkt for high-volume markets
4. **Student-t tails** (`weather_estimator.py`) — `scipy.stats.t.sf(z, df=7)` replaces Gaussian CDF; correctly handles heatwave extremes
5. **AR(1) bias correction** (`weather_estimator.py`) — fetches yesterday's (forecast, actual) from Open-Meteo archive; applies `φ=0.4 × e_{t-1}` to today's forecast. Live from first run.

### What We Fixed Today

1. **σ 2.5°F → 4.0°F** (`weather_estimator.py`) — empirical calibration from actual vs forecast error
2. **Formal Itô RN drift** (`logit_jd.py`) — replaced heuristic with `0.5·σ_b²·tanh(x/2)·τ/24`
3. **Predictions table** (DB) — Brier tracking: `predictions(ticker, trade_date, side, predicted_p, actual_outcome)`
4. **Daily P&L circuit breaker** (`risk/manager.py`) — reads executions table, sets `data/halt.flag` if daily spend ≥ $250
5. **Status bug fix** (`index.py`) — Kalshi uses `'active'` not `'open'`; ALL 983 markets were being skipped
6. **Tau guard** (`index.py`) — skip markets with τ < 6h; same-day markets reflect observed temps, not forecasts

### Today's Demo Trades (2026-04-21)
13 fills — all APR22 (except 3 APR21 that slipped through before tau guard):

| Ticker                      | Side | Buy @ | Qty |
|-----------------------------|------|-------|-----|
| KXHIGHPHIL-26APR22-B66.5   | NO   | 74c   | 2   |
| KXHIGHPHIL-26APR22-B64.5   | NO   | 81c   | 2   |
| KXHIGHDEN-26APR22-B83.5    | NO   | 69c   | 2   |
| KXHIGHDEN-26APR22-B81.5    | NO   | 74c   | 2   |
| KXHIGHDEN-26APR22-B79.5    | NO   | 82c   | 2   |
| KXHIGHDEN-26APR21-B81.5    | NO   | 70c   | 2   |
| KXHIGHLAX-26APR22-B70.5    | NO   | 83c   | 2   |
| KXHIGHLAX-26APR21-B69.5    | NO   | 5c    | 2   | ← bought before tau guard added
| KXHIGHMIA-26APR22-B77.5    | NO   | 79c   | 2   |
| KXHIGHCHI-26APR22-T76      | NO   | 95c   | 2   |
| KXHIGHCHI-26APR22-B73.5    | NO   | 94c   | 2   |
| KXHIGHCHI-26APR21-T73      | YES  | 47c   | 2   | ← today, settling tonight
| KXHIGHCHI-26APR21-B75.5    | NO   | 80c   | 2   | ← today, settling tonight

Check tomorrow:
```bash
python scripts/check_outcomes.py --date 2026-04-21
```

---

### Evening Fixes (ROI focus + Brier validation)

**APR20 outcome confirmed:** -$4.10 on 9 settled markets.
Root cause: 3 correlated LAX bets (T71 YES + B70.5 NO + B68.5 NO) on the same underlying temperature. LAX landed at ~70.8°F — hit the B70.5 band and missed T71, giving us a double loss on two of three bets.

**APR21 Brier scores (first real ones):** avg = **0.0429** (n=5, target <0.10 ✅)

| Ticker | Actual | Settled | P(YES) | Brier |
|--------|--------|---------|--------|-------|
| KXHIGHDEN-26APR21-B81.5 | 84.7°F | NO | 8.1% | 0.0065 |
| KXHIGHLAX-26APR21-B69.5 | 68.2°F | NO | 8.7% | 0.0075 |
| KXHIGHMIA-26APR21-B81.5 | 77.4°F | NO | 9.0% | 0.0081 |
| KXHIGHCHI-26APR21-B75.5 | 73.8°F | NO | 8.4% | 0.0071 |
| KXHIGHCHI-26APR21-T73   | 73.8°F | **YES** | 57.0% | 0.1854 |

**What was shipped:**

1. **City+date deduplication** (`index.py`) — Two-pass: collect all candidates, deduplicate to best-EV per city+date slot. APR22 run: 8 candidates → 5 unique slots. LAX now gets exactly 1 bet, not 3.
2. **Minimum edge filter** (`index.py`) — Only trade when our P(YES) differs from market-implied by ≥ 15¢. Eliminates marginal bets where the model has no real edge.
3. **Brier writeback fixed** (`check_outcomes.py`) — Correctly reads `status=finalized, result=yes/no` from Kalshi REST. Brier summary prints every run.
4. **AR(1) cache bug fixed** (`weather_estimator.py`) — Cache hit was returning `dict` instead of `float`, crashing `estimate_p_yes`. Fixed.
5. **Historical bootstrap done** — 90-day history ingested: 42,751 `weather_actuals` rows, 59,969 `weather_data` rows across 19 cities.
6. **calibrate_sigma.py** — Weekly calibration report: AR(1) φ per city, σ MLE, Brier by city/horizon.

---

## Session: 2026-04-20

### What We Built Today

#### 1. ML Brain (`src/brain/`)
- **`protocol.py`** — `BrainModel` Protocol (runtime-checkable)
- **`logit_jd.py`** — `LogitJumpDiffusionBrain`: Logit Jump-Diffusion model
  - Prior in logit-space from `P_adj_YES` (particle filter / weather estimator)
  - Adaptive market weight `alpha_mkt ≤ 0.40` (our model always has ≥ 60% weight)
  - Risk-neutral drift: `−0.5 · σ_b² · min(τ,24)/24`
  - Staleness shrinkage: `x_post *= (1 − 0.3·pi_stale)`
  - Output clipped to `[0.02, 0.98]`
- **`weather_estimator.py`** — Independent P(YES) prior from Open-Meteo
  - Supports 3 ticker formats: `HIGH_BAND`, `HIGH_ABOVE`, `HOURLY_ABOVE`
  - 19 cities mapped to lat/lon/timezone
  - Gaussian model: `P(T > threshold) = Φ((forecast − threshold) / 2.5°F)`

#### 2. Decision Engine (`src/decision/engine.py`)
- Full rewrite: evaluates both YES and NO sides across full price range
- Official Kalshi fee formula: `ceil(0.07 · C · P · (1−P) · 100)` cents
- Kelly criterion capped at `max_kelly_fraction=0.15`
- Fires when: `EV_net > min_edge_cents=2` AND `total_EV > min_total_ev=3`
- Two reasons: `ARBITRAGE_FOUND` (guaranteed edge), `BRAIN_EV` (model edge)

#### 3. Index / Trade Cycle (`src/index.py`)
- Uses `get_weather_markets()` (1073 markets, not 73k all-markets)
- `_normalize_market()`: converts `yes_ask_dollars` strings → integer cents
- `_build_posterior()`: async, calls `estimate_p_yes()` per ticker for weather prior
- `TEST_MAX_QTY = 2`: caps each order at 2 contracts during testing

#### 4. Tests (`tests/test_brain.py`)
- 17 tests, all passing
- Brier score calibration: `< 0.25` target met on synthetic test set
- Covers: protocol compliance, directional behaviour, staleness, calibration,
  arbitrage detection, both-sides evaluation, Kelly bounding

#### 5. Bug Fixes
- `signal_builder.py`: moved `import numpy as np` out from after a `return` statement
- `pipeline.py`: added missing `import math`
- `particle_filter.py`: added `apply_forecast_jump_blend()` method
- `kalshi_client.py`: added `get_order_book()`, fixed `submit_order()` signature
- Open-Meteo: fixed mutually-exclusive param bug (`start_date` + `past_days` → error)

### Today's Demo Trades (2026-04-20)
12 fills on the Kalshi demo account:

| Ticker                      | Side | Buy @ | Qty |
|-----------------------------|------|-------|-----|
| KXHIGHPHIL-26APR20-B55.5   | NO   | 77c   | 2   |
| KXHIGHPHIL-26APR20-B51.5   | NO   | 78c   | 2   |
| KXHIGHCHI-26APR21-T73       | YES  | 55c   | 2   |
| KXHIGHCHI-26APR20-T56       | NO   | 50c   | 2   |
| KXHIGHCHI-26APR20-B53.5    | NO   | 85c   | 2   |
| KXHIGHCHI-26APR20-B51.5    | NO   | 80c   | 2   |
| KXHIGHLAX-26APR21-B69.5    | NO   | 72c   | 2   |
| KXHIGHLAX-26APR21-B67.5    | NO   | 75c   | 2   |
| KXHIGHLAX-26APR20-T71       | YES  | 54c   | 2   |
| KXHIGHLAX-26APR20-B70.5    | NO   | 89c   | 2   |
| KXHIGHLAX-26APR20-B68.5    | NO   | 69c   | 2   |
| KXHIGHMIA-26APR20-T79       | YES  | 23c   | 2   |

To check settlement tomorrow:
```bash
python scripts/check_outcomes.py --date 2026-04-20
```

---

## Notebook Validation (Quant Desk Simulation notebook)

The notebook confirmed our approach is mathematically sound and suggested these improvements (ordered by priority):

### Priority Improvements for Next Session

#### 1. Real Brier Score Tracking (HIGH — do first)
- After settlements come in, compute Brier score on actual outcomes
- Store per-ticker predictions at trade time → compare to settlement result
- Add `predictions` table: `ticker, predicted_p, actual_outcome, trade_date`
- Recalibrate σ_belief if Brier > 0.20

#### 2. Dynamic alpha_mkt (HIGH)
- Current: fixed `kappa_mkt=0.3` regardless of market volume/depth
- Better: `alpha_mkt = kappa_mkt · (1 − spread_penalty − depth_penalty) · vol_factor`
- `vol_factor = log(1 + volume/100) / log(1 + 1000/100)` (normalised to [0,1])
- This makes the engine trust liquid markets more automatically

#### 3. Heteroskedastic Kalman Filter (MEDIUM)
- Replace fixed σ_belief with time-varying `σ(t)` estimated from residuals
- State: `[x_t, σ_t]`, measurement: market mid in logit space
- Bridges particle filter ↔ brain cleanly

#### 4. Formal RN Drift (MEDIUM)
- Current: `−0.5 · σ_b² · τ/24` (approximate)
- Notebook formula: `μ(t,x) = −0.5 · S''(x)/S'(x) · σ_b²`
  where `S(x) = sigmoid(x)`, so `S''(x)/S'(x) = tanh(x/2)`
- Implement: `rn_drift = −0.5 · σ_b² · tanh(x_post / 2) · min(tau_hrs, 24) / 24`

#### 5. Wire SMC Particle Filter (MEDIUM)
- `src/layer2/particle_filter.py` is already built but not connected to the live cycle
- Feed it real-time Open-Meteo forecasts → produces `posterior_var_T` properly
- Replace the static `posterior_var_T=1.5` in `_build_posterior()`

#### 6. QLIKE Loss for σ Calibration (LOW — long term)
- After 50+ settled markets, fit σ_belief to minimize QLIKE loss
- `QLIKE = σ^{-2}(p − p̂)^2 + log(σ^2)`

---

## Architecture Status

| Component                        | Status                                         |
|----------------------------------|------------------------------------------------|
| Kalshi REST + WebSocket          | ✅ Complete                                    |
| Weather ingestion (Open-Meteo)   | ✅ Complete (90-day bootstrap done)            |
| Database schema (11 tables)      | ✅ Complete (+ weather_actuals, ar1_residuals) |
| EV engine + fee model            | ✅ Complete                                    |
| ML Brain (LogitJD)               | ✅ Complete                                    |
| Weather estimator (prior)        | ✅ Student-t, AR(1), σ=4.0°F                   |
| Decision engine (brain-wired)    | ✅ Fee-corrected Kelly, 0.25× fractional       |
| Trade outcome checker            | ✅ `scripts/check_outcomes.py`                 |
| Brier tracking                   | ✅ First scores: avg 0.0429 (n=5)             |
| Calibration script               | ✅ `scripts/calibrate_sigma.py`               |
| City+date deduplication          | ✅ One best-EV bet per city per day            |
| Minimum edge filter (15¢ static) | ✅ Live in index.py — to be replaced by AS    |
| Risk checks + daily halt         | ✅ Wired to DB, halt.flag support              |
| Fill polling after submit        | ✅ 2s poll × 30s timeout → cancel on expiry   |
| SMC Particle Filter              | ✅ Wired — σ_eff scales with PF variance      |
| 8-gate gating logic              | ⚠️ Built (`src/layer2/`), not wired           |
| Inventory-aware reservation price| ❌ Missing — Avellaneda-Stoikov r_x formula   |
| QLIKE loss for σ_b calibration   | ❌ Missing — needed to calibrate belief vol   |
| Jump compensator in drift        | ❌ Missing — discrete forecast shocks not modeled |
| Per-city σ MLE (calibrated)      | ❌ Need ≥14 days residuals per city            |
| Per-city AR(1) φ (OLS fitted)    | ❌ Need ≥14 days residuals per city            |
| Backtest framework (LOB-aware)   | ❌ Missing                                     |
| Vine Copula cross-city portfolio | ❌ Missing — long-term research                |
| requirements.txt                 | ❌ Missing                                     |

---

## Session: 2026-04-23 (morning — two critical bugs fixed)

### APR22 Settlement Results
15 settled / 87 fills | Net: **+$3.86**
72 APR23 fills pending (settle today — LAX, DEN, CHI, MIA, THOU bands and above)

### Critical Bugs Found & Fixed

**Bug 1 — Dedup not persisting across trade cycles (`src/index.py`)**
- Root cause: in-memory dedup (`best_per_slot`) reset every 5-min cycle → same markets re-entered dozens of times
- Evidence: KXHIGHLAX-26APR23-B74.5 accumulated qty=38 (19 orders), KXHIGHDEN-26APR23-B64.5 qty=20, etc.
- Fix: load `get_open_positions(env_mode)` before Phase 2 dedup; build `already_held` set of city+date slots; skip any slot already in DB
- Confirmed working: "Already held: 24 slots skipped | Filled: 0" on first clean cycle

**Bug 2 — `_p_above` formula sign-inverted (`src/brain/weather_estimator.py`)**
- Root cause: `z = (forecast - threshold)/sigma` → `sf(z)` gives P(T < threshold) NOT P(T > threshold)
- Evidence: CHI T76 with forecast=67.4°F returned P(YES)=96.6% (should be 3.4%). 53 predictions logged at ~99% for a market that settled NO → Brier jumped to 0.5471
- Fix: `z = (threshold - forecast)/sigma` so `sf(z) = P(T_7 > z) = P(actual > threshold)` ✓
- Verified: CHI T76=3.4%, CHI T73=54.8%, DEN B81.5=7.5%, LAX B69.5=8.6% (all match historical records)
- Also explains why YES bets were being placed on markets with forecasts well below threshold (model saw 99% → bet YES → wrong)

**Brier score status:** 0.5471 (inflated by 53 duplicate T76 predictions with ~99% p). New predictions from APR23 forward will be correct.

### Architecture Status Update
| Component | Status |
|-----------|--------|
| Trade cycle dedup (cross-cycle) | ✅ Fixed |
| `_p_above` probability formula | ✅ Fixed |
| APR23 duplicate positions | Settling today — dedup blocks new entries |

---

## Session: 2026-04-22 (evening — Docker + sell-back + all 7 quick wins)

### All 7 Priority Items: DONE ✅

| # | Item | Status |
|---|------|--------|
| 1 | APR22 outcome check | ✅ Done |
| 2 | Avellaneda-Stoikov reservation price (replaces static 15¢) | ✅ Done |
| 3 | Jump compensator wired (PF `apply_forecast_jump_blend`) | ✅ Done |
| 4 | 8-gate gating logic wired into trade_cycle | ✅ Done |
| 5 | QLIKE added to `calibrate_sigma.py` | ✅ Done |
| 6 | Full-spectrum Brier (all edge-filter passers logged) | ✅ Done |
| 7 | `requirements.txt` created + polars added | ✅ Done |

### Additional Work Done This Session

- **`bot_runner.py`** — Docker continuous loop: `trade_cycle` every 5min + `monitor_positions` every 2min + WebSocket auto-restart. Single shared DB/pipeline.
- **`monitor_positions()`** — Exit engine: PROFIT_TARGET (30%), EXPIRY_CLEANUP (<2h any profit), STOP_LOSS (-50% within 4h of expiry). Calls `close_position` via real Kalshi sell order.
- **`ExecutionManager.close_position()`** — IOC sell order, same poll pattern as buy.
- **`DWTraderDB`** — 4 new methods: `get_open_positions`, `update_position_pnl`, `log_position_close`, `log_execution_record`.
- **Docker Compose** — `layer1-ingest` + `kalshi-trader` both healthy, 18+ min with no crashes. Full trade lifecycle confirmed (1 real sell order submitted to Kalshi demo).
- **Critical bug fixes**: `import math` missing, `Config.ENV_EXECUTION_MODE` not defined, WebSocket task spawning 288×/day, sell fills invisible to circuit breaker, duplicate sell guard.

### What Still Needs Attention (audit-validated 2026-04-22)

Audit confirmed: all DONE items verified in code. Items below confirmed MISSING by code inspection.

**IMMEDIATE — unblocks everything else**

| Item | File(s) | Blocker reason |
|------|---------|----------------|
| **PnL attribution** | `analytics/pnl_decomposition.py` (new) + `trade_attribution` DB table | Cannot diagnose losses. Confirmed: no table, no scripts, no joins. `executions`+`predictions`+`scans` tables exist but are never joined. |
| **Add `horizon_hrs` to `ar1_residuals` log** | `src/index.py` Phase 1c + `src/db/dwtrader.py` | Prerequisite for horizon-σ fit. Currently `ar1_residuals` has no horizon column. `tau_hrs` is available at log time in `posterior` dict — just not being stored. |

**HIGH — structural bugs, confirmed by audit**

| Item | File(s) | Evidence from audit |
|------|---------|---------------------|
| **T_max extreme value correction** | `src/layer2/particle_filter.py` + `src/index.py:_run_pf_variance()` | CONFIRMED BUG: `pf.particles` at line 124 is T at resolution_time. KXHIGH settles on daily max. Fix: track running max in propagation loop OR apply Gumbel approximation at end. |
| **Horizon-conditioned σ²(h)** | `scripts/calibrate_sigma.py` + `src/brain/weather_estimator.py` | CONFIRMED: flat `_FORECAST_SIGMA_F=4.0` everywhere. `calibrate_sigma.py` itself says "per-horizon MLE not yet available." Requires `horizon_hrs` in `ar1_residuals` first (item above). |
| **Kalman bias filter** | `src/brain/bias_filter.py` (new) | CONFIRMED MISSING: single-lag AR(1) with fixed φ. No adaptive correction. |

**HIGH — measurement & safety**

| Item | File(s) | Evidence from audit |
|------|---------|---------------------|
| **LVR metric** | `scripts/compute_lvr.py` (new) | CONFIRMED MISSING. `executions` has `price_cents`; `scans` has `best_bid`/`best_ask`. Join possible. Need `lvr_cents` column in `executions`. |
| **Jump-compensated RN drift** | `src/brain/logit_jd.py` | CONFIRMED: drift is diffusion-only `0.5·σ²·tanh(x/2)·τ/24`. No Lévy integral. PF jump blend ✅ handles particle refresh but brain pricing drift is still wrong during anchor jumps. |
| **Kelly + Brier uncertainty penalty** | `src/layer2/ev_engine.py` | Confirmed: fixed 0.25× multiplier. Brier score is computed and stored in `predictions` — just not wired into sizing. |

**MEDIUM — improvements once measurement is working**

| Item | File(s) | Notes |
|------|---------|-------|
| **KL Projection Gap gate** | `src/layer2/gating_logic.py` | 8 gates confirmed, no KL gap. Proxy: `ESS/N × log(1 + spread/fragility)`. |
| **Microstructure alpha layer** | `src/layer2/microstructure_features.py` (new) | `orderbook_events` table EXISTS with raw WebSocket data — data source is available. |
| **Vine Copula CVaR cap** | `scripts/vine_copula.py` (new) | Needs 14d `ar1_residuals` per city pair. Currently 7 cities have ≥1 day — not ready yet. |

**RESEARCH / FUTURE**

| Item | Notes |
|------|-------|
| Tail stress test script | `scripts/stress_test.py` — shift all forecasts ±5°F, recompute portfolio PnL |
| Non-Markovian path signatures | Long-term; requires significant data and architecture change |
| SCF Equilibrium | Multi-agent; out of scope |
| Backtest framework (LOB-aware) | `orderbook_events` is accumulating; build after measurement fixed |

**Already done ✅**

| Item | Notes |
|------|-------|
| Sell IOC fill rate | `sell_price = max(1, current_bid - 2)` — `src/index.py:489` |
| Trade cycle parallelization | `asyncio.gather()` — Phase 1b |
| Per-city σ MLE | `db.get_sigma_mle()` + `load_city_params()` — wired through PF and P(YES) |
| Per-city AR(1) φ | OLS via `get_ar1_phi_estimate()` — wired through `estimate_p_yes()` |
| AR(1) residuals unconditional logging | All 19 cities logged every cycle, not gated by edge filter |
| A-S inventory-aware edge filter | `r_x(t) = x_t - q_t·γ·σ²·(T-t)` — `src/index.py:325` |
| Jump compensator (PF) | `apply_forecast_jump_blend()` wired in `_run_pf_variance()` |
| 8-gate gating logic | Wired in Phase 1d — EV, stale, spread, fragility, ESS, depth, tau, variance |
| Full-spectrum Brier | All edge-filter passers logged to `predictions` table |
| QLIKE in calibrate_sigma.py | Alongside Brier for settled predictions |
| Backtest framework | FUTURE | Both: LOB-aware, stored orderbook snapshots needed |

---

## Notebook Audit #3 — 2026-04-22 (Quant Desk Simulation — after parallelization + per-city calibration)

Submitted full updated architecture (parallelized cycle, per-city σ/φ, unconditional residual logging, A-S edge, 8-gate wired, full-spectrum Brier, QLIKE). Notebook ranked gaps by live PnL impact.

### What the notebook confirmed ✅ (Audit #3)
| Component | Verdict |
|-----------|---------|
| Itô RN drift `0.5·σ_eff²·tanh(x/2)·τ/24` | Correct continuous-diffusion term |
| PF jump blend wired (`apply_forecast_jump_blend`) | Correct — particle population refresh on anchor shift |
| A-S inventory-aware edge filter | Correct — prevents over-concentration |
| City+date dedup | Correct |
| Full-spectrum Brier + QLIKE | Correct — no selection bias, proper scoring |
| Per-city σ MLE + AR(1) φ | Correct — auto-calibrates as data accumulates |

### Gaps identified by notebook (Audit #3, ranked by PnL impact)

**GAP 1 — Vine Copula for cross-city tail dependence (⬆️ upgraded to HIGHEST)**
City dedup removes intra-city correlation but not inter-city. A national heatwave resolves LAX, PHX, DAL simultaneously — Gaussian independence model under-predicts this by 2–5×. Need Student-t Vine Copula to compute CVaR across city pairs and cap total exposure before a correlated event.
- Add: `scripts/vine_copula.py` — fit Student-t pair copulas on `ar1_residuals` per city pair
- Wire into position sizer: if CVaR(portfolio) > threshold, scale down new Kelly qty
- Data requirement: same 14-day residuals already accumulating

**GAP 2 — Jump-compensated RN drift in LogitJD brain (HIGH — systematic mispricing)**
Our PF refreshes particles on anchor jumps ✅. But the brain's drift formula is still pure diffusion. The correct Q-martingale drift requires the Lévy integral:
```
μ(t,x) = [ -0.5·S''(x)·σ_b² - ∫(S(x+z) - S(x) - S'(x)·χ(z))·ν(dz) ] / S'(x)
```
Without it the bot systematically under-prices the "gap risk" premium during 6h forecast windows.
- File: `src/brain/logit_jd.py` — add jump measure `ν(dz)` as calibrated from anchor-shift history
- Calibrate λ_t (jump intensity) and σ_jump from `_prev_mu_cache` history in `src/index.py`

**GAP 3 — Loss-Versus-Rebalancing (LVR) metric (HIGH — measurement gap)**
Brier and QLIKE measure probability accuracy. LVR measures whether informed flow is systematically trading against us:
```
LVR_t = (fill_price - reference_price) × qty   [negative = adverse selection]
```
If cumulative LVR is consistently negative while Brier is good, the strategy is leaking to toxic flow regardless of model quality. Without this metric we cannot distinguish "bad model" from "good model + adversarial counterparties."
- Add: `scripts/compute_lvr.py` — join `executions` + `scans` on ticker+timestamp, compute fill vs. mid at time of fill
- Add `lvr_cents` column to `executions` table

**GAP 4 — KL Projection Gaps in 8-gate filter (MEDIUM — adversarial flow blind spot)**
Gate 8 (posterior_var) is a weak proxy for market identifiability. The notebook standard is to compute KL divergence between P(YES | informed) and P(YES | noise) conditional laws. If KL gap < threshold, the market's signal is not separable from adversarial mimicry and should be skipped.
- File: `src/layer2/gating_logic.py` — add `kl_gap` gate using ESS + spread as proxy until LOB depth is available
- Threshold: KL gap < 0.05 nats → skip (market unidentifiable)

**GAP 5 — Non-Markovian path geometry (RESEARCH)**
AR(1) and OU-PF are Markovian — they only use the current state. Weather fronts are path-dependent; the trajectory of a front carries more information than its current endpoint. Path Signatures / Neural RDEs would give time-resolution invariance. Long-term research item; requires significant historical data.

**GAP 6 — SCF Equilibrium (RESEARCH)**
Self-Consistent Field synchronization: ensures internal path-law proxy is an honest representation of the stochastic ensemble. Relevant if/when we move to multi-agent or neural brain. Out of scope for now.

---

## Next Session Action Plan (2026-04-22, post Audit #3)

### Priority 1 — LVR metric (build first — unblocks diagnosis)
Without LVR we are flying blind. Easy to build from existing data.
- Add `lvr_cents` to `executions` table (migration: `ALTER TABLE executions ADD COLUMN lvr_cents REAL`)
- Populate in `TradeLogger.log_execution_fill()`: `lvr = fill_price - scan_mid_at_time`
- Add `scripts/compute_lvr.py`: sum LVR by city, by day, vs. Brier side-by-side
- **Decision rule**: if 7-day rolling LVR < -50¢/trade AND Brier < 0.10 → adversarial flow, widen edge filter

### Priority 2 — KL Projection Gap gate
- File: `src/layer2/gating_logic.py`
- Add gate: `kl_gap = ESS / N_particles * math.log(1 + spread / max(1, fragility))`
- If `kl_gap < 0.05` → gate fail, log reason `"KL_GAP_LOW"`
- Threshold tunable via `GATE_KL_MIN = 0.05` constant

### Priority 3 — Jump measure calibration for RN drift
- File: `src/brain/logit_jd.py`
- Extract anchor-shift history from `_prev_mu_cache` (already populated each cycle)
- Fit: λ = E[1(|Δanchor| > 1°F)] per city per day; σ_jump = std(Δanchor | jump)
- Add Lévy integral term to drift: approximate as `lambda_t * (sigma_jump^2 / 2) * S''(x) / S'(x)`
- Store calibrated (λ, σ_jump) per city in DB or flat file

### Priority 4 — Vine Copula CVaR cap
- File: `scripts/vine_copula.py` (new) + wire into `src/index.py` Phase 3
- Fit bivariate Student-t copulas on city-pair residuals (19 cities = 171 pairs, prune by correlation)
- Before executing a trade: compute marginal CVaR contribution at 95th percentile
- If portfolio CVaR > $50 → scale Kelly qty down proportionally
- Data requirement: 14+ days of `ar1_residuals` (same data already accumulating)

### Priority 5 — Backtest framework (once LOB snapshots accumulate)
- Needs stored orderbook snapshots from `ws_client`
- Simulate fills at LOB depth, not mid
- Report Sharpe + max drawdown vs. benchmark

---

## QA Desk Review — 2026-04-22 (External Quant Review)

Submitted same architecture brief to an independent quant desk reviewer. Three findings here are **not in the notebook** and are critical structural bugs, not calibration tweaks.

### What QA confirmed overlaps notebook ✅
- Vine Copula / correlation risk: cities not independent, heat wave = correlated wipeout
- Jump compensator for RN drift: still needed in brain (PF refresh ≠ pricing drift fix)
- Kelly too aggressive when model is miscalibrated

### NEW findings not in notebook (ranked by PnL impact)

**QA GAP 1 — Wrong target variable: T(t) vs T_max (CRITICAL — structural bug)**
The particle filter propagates OU on temperature T(t), but Kalshi's `KXHIGH` contracts settle on the **daily maximum** temperature — a path-dependent extreme value, not a point in time.

Current code models: `P(T_resolution > thresh)` — wrong  
Should model: `P(max_{t ∈ day} T(t) > thresh)` — extreme value distribution

Consequence: systematically underprice high-temp contracts, underestimate tail probability on upper bins.

Fix in `src/layer2/particle_filter.py`:
```python
# After OU propagation over the full day, transform particles:
T_max_particles = np.max(T_path_particles, axis=1)  # keep path, take daily max
```
Approximation without full path: `T_max ≈ μ + σ · Z_EV` where `Z_EV` ~ Gumbel(0,1).

Expected PnL impact: **+20–40% in tail contracts**. The QA called this "huge in tails."

**QA GAP 2 — Horizon-conditioned variance (HIGHEST PRIORITY per QA)**
We use flat σ=4.0°F for all forecast horizons. A 6h-ahead forecast has much tighter uncertainty than a 48h forecast. The error variance grows nonlinearly with horizon:

```
σ²(h) = σ₀² + α·h + β·h²
```

Current code: `_p_above(forecast + ar1, thresh, sigma=4.0)` — same σ for all h.

Fix in `scripts/calibrate_sigma.py`: fit (σ₀, α, β) per city from `ar1_residuals` grouped by `horizon_bin`. Then wire into `estimate_p_yes(sigma_f=horizon_sigma(tau_hrs))`.

Expected PnL impact: **+15–30% Sharpe**. QA called this "the most important fix in your stack right now."

**QA GAP 3 — PnL attribution table — you cannot decompose losses (IMMEDIATE)**
Currently all we observe is final outcome (win/loss). We cannot tell if a loss came from:
- bad model (P(YES) wrong)
- bad execution (slippage)
- fees
- adverse selection (toxic flow)

Without decomposition, iteration is blind guessing.

Required: `trade_attribution` table
```
trade_id, predicted_prob, market_price, realized_outcome,
expected_value, realized_value, slippage_cents, fees_cents,
holding_time_hrs, horizon_bin, city, temp_band
```

Files to add:
- `analytics/pnl_decomposition.py` — joins `executions` + `predictions` + `orders` on trade
- `analytics/calibration_report.py` — calibration curve: avg predicted_p vs actual win rate per bucket

**QA GAP 4 — AR(1) → Kalman bias filter (HIGH)**
Single-lag AR(1) lags turning points and overcorrects in stable regimes. Weather errors are regime-dependent (fronts, storms, stable) and non-linear.

Replace `correction = phi * e_{t-1}` with adaptive Kalman filter:
```python
# bias_filter.py
bias_t = bias_{t-1} + K * (actual - forecast - bias_{t-1})
# K adapts: high during transitions, low in stable periods
```

File: new `src/brain/bias_filter.py`, replaces `_fetch_ar1_correction()` in `weather_estimator.py`.

Expected PnL impact: **+10–20%** via better turning-point capture.

**QA GAP 5 — Microstructure alpha layer (MEDIUM — monetizable edge)**
We treat Kalshi quotes as passive. Prediction markets have stale quotes, retail flow, and asymmetric liquidity. Desks monetize this:

```
order_imbalance = bid_size / (bid_size + ask_size)
quote_age = now - last_update_time
spread_zscore = (spread - spread_ma) / spread_std
EV_adj = EV + lambda_micro * micro_signal
```

File: `src/layer2/microstructure_features.py`
Source: WebSocket orderbook feed already collected in `orderbook_events` table.

**QA GAP 6 — Kelly unsafe with model uncertainty (MEDIUM)**
`0.25 × Kelly` is still too aggressive when σ is miscalibrated. Replace fixed multiplier with uncertainty-penalized sizing:
```
f = edge / (variance + model_uncertainty)
```
Where `model_uncertainty` = Brier score on last N settled predictions (already computed).

File: `src/layer2/ev_engine.py` — add `brier_penalty` param to Kelly formula.

**QA GAP 7 — No tail stress testing (MEDIUM)**
No scenario analysis. Need: compute portfolio PnL if all forecasts shift +5°F / -5°F simultaneously.
File: `scripts/stress_test.py`

### QA Priority Order (their ranking)
| # | Item | File | QA Impact |
|---|------|------|-----------|
| 1 | Horizon-aware σ²(h) | `calibrate_sigma.py` + `weather_estimator.py` | +15–30% Sharpe |
| 2 | PnL attribution + calibration dashboards | `analytics/` | Unblocks all diagnosis |
| 3 | T_max extreme value correction in PF | `particle_filter.py` | +20–40% in tail contracts |
| 4 | Kalman bias filter (replace AR1) | `src/brain/bias_filter.py` | +10–20% |
| 5 | Microstructure alpha layer | `src/layer2/microstructure_features.py` | Turns neutral → profitable |
| 6 | Portfolio covariance / correlation cap | `src/risk/covariance.py` | Prevents blow-ups |
| 7 | Backtest engine | `scripts/backtest.py` | Only after measurement fixed |

### Merged Priority List (Notebook + QA, deduplicated)
| Priority | Item | Source | Urgency |
|----------|------|---------|---------|
| 1 | Horizon-conditioned σ²(h) | QA only | **BUILD NOW** |
| 2 | PnL attribution table + analytics | QA only | **BUILD NOW** |
| 3 | T_max extreme value PF correction | QA only | **BUILD NEXT** |
| 4 | Kalman bias filter (replace AR1) | QA only | HIGH |
| 5 | LVR metric | Notebook only | HIGH |
| 6 | Jump-compensated RN drift (Lévy integral) | Both | HIGH |
| 7 | KL Projection Gap gate | Notebook | MEDIUM |
| 8 | Microstructure alpha layer | QA only | MEDIUM |
| 9 | Kelly + Brier uncertainty penalty | QA only | MEDIUM |
| 10 | Vine Copula CVaR cap | Both | MEDIUM (needs 14d data) |
| 11 | Tail stress test script | QA only | LOW |
| 12 | Backtest framework (LOB-aware) | Both | FUTURE |
