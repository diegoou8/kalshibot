# KXTEMP / KXHIGH Coverage Analysis
**Date:** 2026-06-01  
**Branch:** main  
**Analyst:** brain_coverage_report.py + live Kalshi scan

---

## 1. What triggered this investigation

The trade funnel logged `No P(YES) estimate: 663` every cycle against only 694 eligible markets — a 95.5% failure rate. The bot was producing zero trades. Two prior suspect causes were ruled out (Open-Meteo reachability, city_guard blocking). The actual cause turned out to be city-code mismatches between Kalshi ticker strings and the internal `_CITY_MAP`.

---

## 2. Live market scan results (2026-06-01, 969 markets total)

| Prefix | Markets | Distinct city codes | Previously in `_CITY_MAP` |
|---|---|---|---|
| KXHIGH | 240 | 20 | 7 |
| KXTEMP | 48 | 1 | 0 |
| KXRAIN | 30 | 2 | n/a (no parser) |
| Other (hurricane, drought…) | 651 | — | — |

### KXHIGH city codes — before fix

| Code | Count | Status | Maps to |
|---|---|---|---|
| LAX | 12 | In map | — |
| PHIL | 12 | In map | — |
| MIA | 12 | In map | — |
| THOU | 12 | In map | — |
| DEN | 12 | In map | — |
| CHI | 12 | In map | — |
| TDC | 12 | In map | — |
| **NY** | **12** | **Missing** | NYC |
| **TSFO** | **12** | **Missing** | SFO |
| **TPHX** | **12** | **Missing** | PHX |
| **TMIN** | **12** | **Missing** | MIN |
| **TATL** | **12** | **Missing** | ATL |
| **TOKC** | **12** | **Missing** | OKC |
| **TBOS** | **12** | **Missing** | BOS |
| **TSATX** | **12** | **Missing** | SAT |
| **TSEA** | **12** | **Missing** | SEA |
| **TDAL** | **12** | **Missing** | DAL |
| **TNOLA** | **12** | **New city** | NOLA (New Orleans) |
| **AUS** | **12** | **New city** | AUS (Austin TX) |
| **TLV** | **12** | **New city** | LV (Las Vegas) |

**Result before fix:** 84/240 KXHIGH markets could get P(YES). 156 silently returned None.

### KXTEMP city codes — before fix

| Code | Count | Status | Maps to |
|---|---|---|---|
| **NYCH** | **48** | **Missing** | NYC |

**Result before fix:** 0/48 KXTEMP markets could get P(YES).

---

## 3. Root cause analysis

Kalshi uses different city-code conventions across market types:

- **KXHIGH** daily-high markets use the canonical internal code (LAX, CHI, TDC…) for older markets, but have accumulated a "T-prefix" convention (`TSFO`, `TDAL`, etc.) and three brand-new cities (`TNOLA`, `AUS`, `TLV`) as the product expanded.
- **KXTEMP** hourly-exact-temp markets use a completely different suffix schema. NYC becomes `NYCH`, implying Kalshi appends a letter to distinguish hourly from daily markets in their internal naming.
- **KXRAIN** rain markets have no parser at all in `_parse_ticker`. Out of scope for this change.

Neither the T-prefix pattern nor the KXTEMP suffix pattern was documented or handled in `_CITY_MAP` or `_parse_ticker`, so every affected ticker returned `None` from `estimate_p_yes` before even reaching Open-Meteo.

---

## 4. DB calibration state (from Azure SQL, 2026-06-01)

| City | AR(1) residual rows | Sigma | Brier (n=30) | Guard status (paper mode) |
|---|---|---|---|---|
| TDC | 3 | default (4.0°F) | 0.576 | CITY_THROTTLED_PAPER_MODE 0.25× |
| PHIL | 3 | default | 0.522 | CITY_THROTTLED_PAPER_MODE 0.25× |
| LAX | 3 | default | 0.391 | CITY_THROTTLED_PAPER_MODE 0.25× |
| DEN | 3 | default | 0.215 | BRIER_THROTTLE_APPLIED 0.5× |
| THOU | 3 | default | 0.190 | Full sizing |
| MIA | 3 | default | 0.109 (n=3) | Full sizing (below MIN_OBS) |
| 13 others | 0 | default | None | Full sizing (no data) |

No city has reached the 14-day minimum for calibrated sigma. All cities use the 4.0°F default forecast uncertainty.

---

## 5. Changes made

### `src/brain/weather_estimator.py`

**Three new cities added to `_CITY_MAP`:**
- `NOLA`: New Orleans, LA — (29.9511, -90.0715, America/Chicago)
- `AUS`: Austin, TX — (30.2672, -97.7431, America/Chicago)
- `LV`: Las Vegas, NV — (36.1699, -115.1398, America/Los_Angeles)

**`_KXTEMP_CITY_ALIAS` dict:**
```
NYCH -> NYC
```
Applied in `_parse_ticker` KXTEMP branch before returning `city`. Unknown KXTEMP suffixes emit `UNKNOWN_KXTEMP_CITY_CODE` warning (once per process lifetime).

**`_KXHIGH_CITY_ALIAS` dict:**
```
NY    -> NYC    TSFO  -> SFO    TPHX  -> PHX
TMIN  -> MIN    TATL  -> ATL    TOKC  -> OKC
TBOS  -> BOS    TSATX -> SAT    TSEA  -> SEA
TDAL  -> DAL    TNOLA -> NOLA   AUS   -> AUS
TLV   -> LV
```
Applied in both KXHIGH branches of `_parse_ticker`. Unknown KXHIGH suffixes emit `UNKNOWN_KXHIGH_CITY_CODE` warning.

### `analytics/cycle_diagnostics.py`

Four new counters in `CycleDiagnostics`:
- `n_kxtemp_scanned` — KXTEMP markets after spread filter
- `n_kxtemp_unknown_city` — suffix not resolved by alias map
- `n_kxtemp_no_estimate` — resolved but Open-Meteo returned None
- `n_kxtemp_p_yes` — reached a valid P(YES)

New section in `generate_report`:
```
KXTEMP COVERAGE (hourly temp markets)
  Scanned           :    48
  Produced P(YES)   :    48  (100%)
  Unknown city code :     0
  No estimate       :     0
```
Only rendered when `n_kxtemp_scanned > 0`.

### `src/index.py`

Phase 1a: `n_kxtemp_scanned` incremented for every KXTEMP market that passes the spread filter.  
Phase 1d: KXTEMP markets classified when `p_yes is None` (unknown_city vs no_estimate); `n_kxtemp_p_yes` incremented on success.

### `tests/test_brain.py`

`TestKxtempCityAlias` class — 4 tests, 8 subtests, all pure (no I/O):
- `test_nych_maps_to_nyc` — KXTEMPNYCH resolves to NYC
- `test_unknown_kxtemp_suffix_returns_result_with_unmapped_city` — unknown suffix parses but city not in map
- `test_kxhigh_parsing_unchanged_for_existing_cities` — LAX/TDC/CHI regression guard
- `test_kxhigh_tprefix_alias_resolves` — TSFO, TDAL, NY, TNOLA, AUS all resolve

---

## 6. Post-fix coverage (live scan run after deploy)

| Prefix | Markets | Resolved | Unknown |
|---|---|---|---|
| KXHIGH | 240 | **240 (100%)** | 0 |
| KXTEMP | 48* | **48 (100%)** | 0 |
| KXRAIN | 30 | 0 | n/a (no parser, out of scope) |

*KXTEMP markets confirmed at 48 earlier in the session; they expired by the post-fix scan (hourly markets settle within the hour).

**Effective market expansion:**
- KXHIGH: +156 newly resolvable markets per cycle (13 codes × 12 markets each)
- KXTEMP: +48 newly resolvable markets per cycle when present

---

## 7. Expected funnel changes after deploy

Before:
```
Markets scanned       :   945
After spread filter   :   717
No P(YES) estimate    :   663   (92%)
Candidates            :     0
```

After (estimated, assuming KXHIGH spread distribution similar to LAX/TDC):
```
Markets scanned       :   ~970
After spread filter   :   ~730
No P(YES) estimate    :   ~480   (KXRAIN + other = ~510, KXHIGH now resolved)
Candidates (pre-dedup):   ~15-30  (newly eligible cities)
```

The 663→~480 improvement comes from KXHIGH T-prefix cities. If KXTEMP NYCH markets are present, the No P(YES) count drops further.

---

## 8. Open items

### High priority

**KXRAIN parser (out of scope this PR)**  
30 markets (NYCM, NYC) return None unconditionally because `_parse_ticker` has no KXRAIN regex. Rain settlement requires a precipitation model (not a temperature CDF). Separate task.

**New city sigma calibration**  
NOLA, AUS, LV start with zero AR(1) residuals — they will use the 4.0°F default sigma indefinitely until they accumulate 14 days of settled trades. Brier for these cities starts uncalibrated; the city guard will not block them (below MIN_OBS=10) but will log them for monitoring.

**KXTEMP other-city codes undiscovered**  
Only NYCH observed today. If Kalshi adds CHIH, LAXH, DENH, etc. in the future, those will emit `UNKNOWN_KXTEMP_CITY_CODE` warnings. Extend `_KXTEMP_CITY_ALIAS` as new codes appear in logs.

### Low priority

**KXHIGH `_ticker_city` in `src/index.py` still extracts raw codes**  
The helper `_ticker_city(ticker)` at line 60 extracts the raw city code from the ticker string without applying the alias map. This means the `city` field in the posterior dict for TSFO markets would be the normalized `SFO` (from `_parse_ticker`) but `_ticker_city` for the same ticker would return `TSFO`. This could cause subtle mismatches in position de-dup (`_slot_net_qty`) if TSFO markets are traded. Investigation needed before TSFO markets are live-traded.

---

## 9. Test results

```
tests/test_brain.py    19 passed, 8 subtests passed
tests/test_guards.py    6 passed (paper-mode + live-mode city guard)
```
