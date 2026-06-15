import asyncio
import logging
import math
import re as _re
import uuid
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from src.services.kalshi_client import client
from src.db.dwtrader import DWTraderDB
from src.config.env import Config

from src.decision.engine import DecisionEngine, _kalshi_fee_per_contract
from src.risk.manager import RiskManager
from src.risk.city_guard import CityRiskGuard
from src.risk.segment_guard import SegmentGuard
from src.risk.contract_audit import audit_and_store, get_verified_contract_semantics
from src.types import ContractSemantics
from src.execution.manager import ExecutionManager
from src.logging.trade_logger import TradeLogger
from src.brain.logit_jd import LogitJumpDiffusionBrain
import numpy as np
from src.layer2.particle_filter import TemperatureParticleFilter
from src.layer2.gating_logic import TradeGating
from src.brain.weather_estimator import (
    estimate_p_yes, get_ar1_metadata, get_forecast_temp_for_ticker,
    load_city_params, _CITY_MAP, _FORECAST_SIGMA_F, _AR1_PHI, _parse_ticker,
    normalize_city_code,
)
from analytics.cycle_diagnostics import CycleDiagnostics, compute_strike_z

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] - %(message)s')
logger = logging.getLogger("IndexOrchestrator")

# Max contracts per order during testing
TEST_MAX_QTY = 2

# Probability-edge gate: fair-value P(YES) must differ from market-implied P(YES)
# by at least this many percentage points (pp × 100) before we evaluate EV.
# 15 = 15 pp of edge. This is NOT the same unit as EV cents.
MIN_PROB_EDGE_PP = 15

# Minimum net EV per contract in cents after fees before an order may be submitted.
# Matches DecisionEngine.min_edge_cents. Belt-and-suspenders final guard uses this.
MIN_EV_CENTS = 5

# Per-ticker forecast anchor cache used by the jump compensator.
# Stores the mu_anc from the previous call so apply_forecast_jump_blend can
# detect anchor shifts > 1°F between trade cycles.
_prev_mu_cache: Dict[str, float] = {}

# ── Position exit thresholds ────────────────────────────────────────────────
# Profit target: sell when unrealized gain >= 30% of avg entry cost.
EXIT_PROFIT_TARGET   = 0.30
# Stop-loss: sell when losing >= 50% AND within EXIT_STOP_LOSS_TAU_MAX_HRS of expiry.
EXIT_STOP_LOSS       = -0.50
EXIT_STOP_LOSS_TAU_MAX_HRS = 4.0
# Expiry cleanup: lock in any positive PnL within 2h of settlement rather than
# absorbing binary settlement risk on a position that's already in profit.
EXIT_EXPIRY_CLEANUP_TAU_HRS = 2.0


def _ticker_city(ticker: str) -> Optional[str]:
    """Return the normalized _CITY_MAP key for a KXHIGH/KXTEMP ticker."""
    m = _re.match(r"KX(HIGH|TEMP)([A-Z]+)-", ticker, _re.IGNORECASE)
    if not m:
        return None
    return normalize_city_code(m.group(2).upper(), market_prefix="KX" + m.group(1).upper())


def _ticker_date(ticker: str) -> Optional[str]:
    """KXHIGHCHI-26APR21-T73 → '2026-04-21'"""
    m = _re.match(r"KX(?:HIGH|TEMP)[A-Z]+-(\d{2}[A-Z]{3}\d{2})", ticker, _re.IGNORECASE)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).upper(), "%y%b%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _ticker_strike(ticker: str) -> Optional[float]:
    """KXHIGHCHI-26APR29-B54.5 → 54.5  |  KXHIGHCHI-26APR29-T60 → 60.0"""
    m = _re.search(r"-[BT](\d+(?:\.\d+)?)$", ticker, _re.IGNORECASE)
    return float(m.group(1)) if m else None


def _tau_to_bin(tau_hrs: float) -> str:
    if tau_hrs < 6:   return "0-6h"
    if tau_hrs < 12:  return "6-12h"
    if tau_hrs < 24:  return "12-24h"
    if tau_hrs < 48:  return "24-48h"
    return "48h+"


def _strike_distance_bucket(z: float) -> str:
    """Map signed strike z-score to distance bucket for calibration reporting."""
    if math.isnan(z) or math.isinf(z):
        return "atm"
    if z > 2.0:   return "far_otm"
    if z > 0.5:   return "otm"
    if z > -0.5:  return "atm"
    if z > -2.0:  return "itm"
    return "far_itm"


def _run_pf_variance(
    ticker: str,
    ar1_correction: float,
    tau_hrs: float,
    sigma_city: float = 2.0,
) -> Tuple[float, float]:
    """
    Run the SMC particle filter for one market.
    Returns (posterior_var_T, ess) where ess is the effective sample size.

    Propagates N=400 particles over tau_hrs using the OU model, stepping in
    6-hour chunks.  Before propagation, applies apply_forecast_jump_blend when
    the anchor has shifted >1°F since the previous call (jump compensator).

    sigma_city: per-city σ (°F) used to initialise particle spread. Defaults to 2.0
    when no calibrated value is available (half the 4°F forecast default).
    """
    forecast_temp = get_forecast_temp_for_ticker(ticker)
    if forecast_temp is None or tau_hrs < 1.0:
        return 1.5, 80.0  # fallback: tight var + nominal ESS

    mu_0 = forecast_temp + ar1_correction
    resolution_time_s = tau_hrs * 3600.0

    pf = TemperatureParticleFilter(N=400, sigma_init=sigma_city)
    pf.initialize(mu_anc_0=mu_0, V_anc_0=0.0, current_time=0.0)

    # Jump compensator: if anchor shifted >1°F since last cycle, blend particles
    prev_mu = _prev_mu_cache.get(ticker)
    if prev_mu is not None:
        pf.apply_forecast_jump_blend(prev_mu_anc=prev_mu)
    _prev_mu_cache[ticker] = mu_0

    # Step in 6h chunks; each chunk uses the remaining tau_settle for beta_2 term
    step_hrs = 6.0
    n_steps = max(1, int(tau_hrs / step_hrs))
    for i in range(n_steps):
        t_now = min((i + 1) * step_hrs * 3600.0, resolution_time_s)
        pf.propagate(
            current_time=t_now,
            resolution_time=resolution_time_s,
            mu_anc=mu_0,
            V_anc=0.0,
        )

    mu_pf = float(np.sum(pf.weights * pf.particles))
    var_pf = float(np.sum(pf.weights * (pf.particles - mu_pf) ** 2))

    # KXHIGH contracts settle on the daily maximum temperature, not T at resolution
    # time.  Apply Gumbel correction with the configured mode so this component
    # is consistent with estimate_p_yes which applies the same scaling.
    is_daily_max = "KXHIGH" in ticker.upper()
    if is_daily_max and pf.is_initialized:
        var_pf = pf.daily_max_var(sigma_intraday=2.0, mode=Config.GUMBEL_MODE)

    return max(1.0, var_pf), pf.ess()


def _normalize_market(m: Dict[str, Any]) -> Dict[str, Any]:
    """
    Kalshi REST returns prices as `*_dollars` string values (e.g. '0.3400').
    Convert all price fields to integer cents for consistent engine input.
    """
    for field in ("yes_ask", "yes_bid", "no_ask", "no_bid"):
        dollars_key = f"{field}_dollars"
        # Prefer direct int field; fall back to *_dollars string
        if field not in m or m[field] is None:
            val = m.get(dollars_key)
            if val is not None:
                try:
                    m[field] = int(round(float(val) * 100))
                except (ValueError, TypeError):
                    m[field] = None
        elif m[field] is not None:
            try:
                m[field] = int(round(float(m[field])))
            except (ValueError, TypeError):
                m[field] = None
    return m


async def _build_posterior(
    m: Dict[str, Any],
    city_params: Optional[Dict[str, Dict]] = None,
    db: Optional["DWTraderDB"] = None,
) -> Dict[str, Any]:
    """
    Build a posterior dict for the brain.

    Calls get_verified_contract_semantics(ticker, m) to resolve direction
    from the live Kalshi market dict (which contains strike_type).
    Falls back to the DB-cached kx_strike_type when the bulk markets endpoint
    does not include strike_type (T-type markets always need it).
    Returns semantics in the posterior so Phase 1d can enforce fail-closed.

    city_params: per-city calibrated {sigma, phi} from load_city_params().
    When absent or a city has < 14 days of data, module defaults are used.
    """
    tau_hrs = 12.0
    close_time = m.get("close_time") or m.get("expiration_time")
    if close_time:
        try:
            if isinstance(close_time, str):
                dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            else:
                dt = close_time
            tau_hrs = max(0.25, (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0)
        except Exception:
            pass

    ticker = m.get("ticker", "")

    # Build verified semantics from the live Kalshi market dict.
    # m is from get_weather_markets() and includes strike_type/floor_strike/cap_strike.
    # For T-type daily markets: verified=True when strike_type is present.
    # For BAND/HOURLY: verified=True always (direction unambiguous from ticker).
    try:
        semantics: Optional[ContractSemantics] = get_verified_contract_semantics(ticker, m)
    except Exception as _sem_exc:
        logger.warning("ContractSemantics build failed for %s: %s", ticker, _sem_exc)
        semantics = None

    # DB fallback for DIRECTION_UNKNOWN_NO_METADATA: the bulk /markets?series_ticker=...
    # endpoint does NOT include strike_type in its market objects. The nightly
    # fetch_and_audit_metadata() calls /markets/{ticker} individually and stores
    # kx_strike_type in contract_semantics. Use that cached value as fallback so
    # T-type markets that have been audited at least once remain tradeable.
    if (
        db is not None
        and semantics is not None
        and not semantics.verified
        and semantics.failure_reason == "DIRECTION_UNKNOWN_NO_METADATA"
    ):
        try:
            _cs = db.get_contract_semantics(ticker)
            if _cs and _cs.get("kx_strike_type"):
                _cached_st = _cs["kx_strike_type"]
                _rebuilt = get_verified_contract_semantics(ticker, {"strike_type": _cached_st})
                if _rebuilt.verified:
                    logger.debug("ContractSemantics DB fallback OK: %s → %s", ticker, _rebuilt.direction)
                    semantics = _rebuilt
                else:
                    logger.debug("ContractSemantics DB fallback still unverified: %s (%s)",
                                 ticker, _rebuilt.failure_reason)
        except Exception as _fb_exc:
            logger.warning("ContractSemantics DB fallback error %s: %s", ticker, _fb_exc)

    city = (semantics.canonical_city if semantics else None) or _ticker_city(ticker)

    # Early exit: Phase 1d enforces fail-closed on semantics.verified=False
    if semantics is None or not semantics.verified:
        return {
            "P_adj_YES":       None,
            "semantics":       semantics,
            "posterior_var_T": 0.0,
            "pf_ess":          0.0,
            "tau_hrs":         tau_hrs,
            "pi_stale":        1.0,
            "city":            city,
            "ar1_correction":  0.0,
            "sigma":           _FORECAST_SIGMA_F,
        }

    # Per-city calibrated params (defaults when < 14 days of residuals)
    cparams    = (city_params or {}).get(city or "", {})
    sigma_city = cparams.get("sigma", _FORECAST_SIGMA_F)
    phi_city   = cparams.get("phi",   _AR1_PHI)

    # estimate_p_yes now takes ContractSemantics — ABOVE/BELOW direction handled inside
    p_adj = await estimate_p_yes(semantics, sigma_f=sigma_city, phi=phi_city, tau_hrs=tau_hrs)

    pi_stale = 0.1 if p_adj is not None else 0.4

    ar1_correction = 0.0
    if city and city in _CITY_MAP:
        lat, lon, _ = _CITY_MAP[city]
        meta = get_ar1_metadata(lat, lon)
        e_prev = meta.get("e_prev", 0.0) if meta else 0.0
        ar1_correction = phi_city * e_prev

    posterior_var_T, pf_ess = _run_pf_variance(
        ticker=ticker,
        ar1_correction=ar1_correction,
        tau_hrs=tau_hrs,
        sigma_city=sigma_city / 2.0,
    )

    return {
        "P_adj_YES":       p_adj,
        "semantics":       semantics,
        "posterior_var_T": posterior_var_T,
        "pf_ess":          pf_ess,
        "tau_hrs":         tau_hrs,
        "pi_stale":        pi_stale,
        "city":            city,
        "ar1_correction":  ar1_correction,
        "sigma":           sigma_city,
    }


def _slot_net_qty(db: DWTraderDB, city: str, settle_date: str, env_mode: str) -> int:
    """Net signed YES exposure for a city/date slot (YES contracts = +qty, NO = -qty)."""
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT ticker, side, qty FROM positions WHERE environment = ?",
            (env_mode.upper(),),
        ).fetchall()
    net = 0
    for r in rows:
        r_ticker, r_side, r_qty = r[0], r[1], int(r[2])
        if _ticker_city(r_ticker) == city and _ticker_date(r_ticker) == settle_date:
            net += r_qty if r_side == "yes" else -r_qty
    return net


async def trade_cycle(env_mode: str):
    logger.info(f"Starting Trade Cycle [{env_mode}]")
    logger.info("GUMBEL_MODE=%s cycle_start", Config.GUMBEL_MODE)
    diag = CycleDiagnostics()

    db           = DWTraderDB()
    brain        = LogitJumpDiffusionBrain(sigma_belief=0.3, kappa_mkt=0.3, max_alpha_mkt=0.40)
    engine       = DecisionEngine(brain=brain, max_kelly_fraction=0.15, kelly_multiplier=0.25, min_total_ev=3.0)
    risk_manager = RiskManager(db)
    executor     = ExecutionManager(client)
    trade_logger = TradeLogger(db)
    gating       = TradeGating()

    # Adaptive city risk guard: evaluates rolling Brier per city, sets blocks/throttles.
    city_guard = CityRiskGuard()
    city_guard.refresh(db, list(_CITY_MAP.keys()), env_mode)

    # Segment guard: city+side+market_type blocking + trust-score shrinkage.
    # Hard blocks: DEN/YES, TDC/YES, PHIL/YES, LAX/NO (seeded from Jun-13 post-mortem).
    # Dynamic: DB segment_performance rows drive trust in range [0.0, 0.5].
    segment_guard = SegmentGuard()
    segment_guard.refresh(db)

    current_balance = Config.BANKROLL

    # Rolling Brier over last 7 days — used to penalize Kelly when model is miscalibrated
    rolling_brier = db.get_rolling_brier(n_days=7)
    logger.info("Rolling 7-day Brier: %.4f (kelly_mult effective=%.3f)",
                rolling_brier, max(0.05, 0.25 / (1.0 + 2.0 * rolling_brier)))

    markets = await client.get_weather_markets()
    if not markets:
        logger.warning("No weather markets returned. Sleeping.")
        return

    diag.n_scanned = len(markets)
    logger.info(f"Evaluating {len(markets)} weather markets with brain.")

    # Load per-city calibrated σ and φ from DB (fast — SQLite only, no I/O).
    # Falls back to module defaults for cities with < 14 days of residuals.
    city_params = load_city_params(db)
    calibrated = [c for c, p in city_params.items() if p.get("calibrated")]
    if calibrated:
        logger.info("Using calibrated params for %d/%d cities: %s",
                    len(calibrated), len(city_params), ", ".join(sorted(calibrated)))

    # ── Phase 1a: Pre-filter markets (cheap, sync) ────────────────────────────
    valid_markets: List[Dict] = []
    for market in markets:
        market = _normalize_market(market)
        ticker  = market.get("ticker")
        yes_ask = market.get("yes_ask", 100)
        no_ask  = market.get("no_ask", 100)
        if not ticker or yes_ask is None or no_ask is None:
            continue
        if market.get("status") not in (None, "open", "active", ""):
            continue
        if yes_ask >= 100 and no_ask >= 100:
            continue
        # Spread filter: wide spread = thin market, high adverse-selection risk
        if (yes_ask + no_ask - 100) > Config.MAX_SPREAD_CENTS:
            continue
        valid_markets.append(market)
        if ticker and ticker.upper().startswith("KXTEMP"):
            diag.n_kxtemp_scanned += 1
    diag.n_spread_ok = len(valid_markets)

    # ── Phase 1b: Build posteriors in parallel ────────────────────────────────
    # All _build_posterior() calls fire simultaneously so Open-Meteo fetches for
    # all ~19 unique cities happen concurrently instead of sequentially.
    # The module-level _forecast_cache and _ar1_error_cache ensure each unique
    # city+date is only fetched once despite concurrent access.
    posteriors_raw = await asyncio.gather(
        *[_build_posterior(m, city_params, db=db) for m in valid_markets],
        return_exceptions=True,
    )

    # ── Phase 1c: Log AR(1) residuals for every city seen this cycle ─────────
    # Must happen BEFORE edge filtering — we need residuals from all cities,
    # not just the ones that happen to have a tradable market today.
    # INSERT OR REPLACE on (city, target_date) keeps exactly one row per city/day.
    ar1_logged: set = set()
    for posterior in posteriors_raw:
        if isinstance(posterior, Exception):
            continue
        city_code = posterior.get("city") or ""
        if city_code and city_code in _CITY_MAP and city_code not in ar1_logged:
            lat, lon, _ = _CITY_MAP[city_code]
            meta = get_ar1_metadata(lat, lon)
            if meta and meta.get("e_prev") is not None:
                db.log_ar1_residual(
                    city=city_code,
                    target_date=meta["yesterday"],
                    forecast_temp_f=meta["forecast_yest"],
                    actual_temp_f=meta["actual_yest"],
                    horizon_hrs=posterior.get("tau_hrs"),
                )
            ar1_logged.add(city_code)

    # ── Phase 1d: Process results (sync — no more I/O bottleneck) ────────────
    Candidate = Tuple[float, Dict, Dict, Any, int]
    candidates: List[Candidate] = []

    for market, posterior in zip(valid_markets, posteriors_raw):
        if isinstance(posterior, Exception):
            logger.debug("Posterior error for %s: %s", market.get("ticker"), posterior)
            continue

        ticker  = market.get("ticker")
        yes_ask = market.get("yes_ask", 100)
        no_ask  = market.get("no_ask", 100)

        scan_id = trade_logger.log_scan_step(market, env_mode)

        # Skip markets expiring < 6h — weather stations already observed
        if posterior.get("tau_hrs", 99.0) < 6.0:
            diag.n_tau_skip += 1
            continue

        # Fail-closed: direction must be verified from Kalshi metadata.
        # semantics was built in _build_posterior from the live market dict.
        # estimate_p_yes already handled BELOW direction — no post-hoc correction.
        _semantics = posterior.get("semantics")
        if _semantics is not None and not _semantics.verified:
            logger.warning(
                "BLOCKED_UNVERIFIED_CONTRACT_SEMANTICS: %s — %s",
                ticker, (_semantics.failure_reason or "unknown"),
            )
            diag.n_no_p_yes += 1
            continue

        p_yes = posterior.get("P_adj_YES")
        if p_yes is None:
            diag.n_no_p_yes += 1
            if ticker.upper().startswith("KXTEMP"):
                _pt = _parse_ticker(ticker)
                if _pt is None or _pt.get("city") not in _CITY_MAP:
                    diag.n_kxtemp_unknown_city += 1
                else:
                    diag.n_kxtemp_no_estimate += 1
            continue

        if ticker.upper().startswith("KXTEMP"):
            diag.n_kxtemp_p_yes += 1

        # ── Avellaneda-Stoikov inventory-aware edge threshold ──────────────────
        # Base threshold (15c) is bumped upward when we already hold exposure in
        # the same city/date slot, reducing willingness to add correlated risk.
        city_code = posterior.get("city") or ""

        # City risk guard: skip blocked cities, apply throttle multiplier to qty
        _city_allowed, _size_mult = city_guard.check(city_code)
        if not _city_allowed:
            logger.info("CITY_BLOCKED_GUARD: %s (%s) — skipping", ticker, city_code)
            continue

        # Contract semantics audit: DB persistence for audit trail.
        # Direction decision is already resolved via semantics — audit_and_store is for DB only.
        _market_type = (_semantics.market_type or "") if _semantics else ""
        audit_and_store(ticker, db)

        # Segment guard: city+side pre-check before EV is computed.
        # Determines both allow/block AND trust score for edge shrinkage.
        # preferred_side computed from raw p_yes vs market to determine which
        # side we would trade — used for segment check (we only need to check
        # the side we intend to take, not both).
        market_implied_yes = yes_ask / 100.0
        market_implied_no  = 1.0 - no_ask / 100.0
        _pref_yes = (p_yes - market_implied_yes) >= (market_implied_no - p_yes)
        _intended_side = "yes" if _pref_yes else "no"

        _seg_allow, _seg_trust, _seg_status = segment_guard.check(
            city_code, _intended_side, _market_type
        )
        if not _seg_allow:
            # BLOCKED_SEGMENT_GUARD already logged inside segment_guard.check()
            continue

        # Market-shrunk edge: replace raw model probability with a convex
        # combination of market price and model estimate, weighted by trust.
        #   p_final = p_market + trust * (p_model - p_market)
        # At trust=0.0: follow market exactly (zero model alpha).
        # At trust=0.5: model gets half the weight (ceiling until proven).
        if _seg_trust < 1.0:
            p_yes_shrunk = market_implied_yes + _seg_trust * (p_yes - market_implied_yes)
            if _seg_status in ("SHADOW_ONLY", "THROTTLE"):
                logger.debug(
                    "EDGE_SHRINK %s: p_model=%.3f p_market=%.3f trust=%.2f "
                    "→ p_final=%.3f [%s]",
                    ticker, p_yes, market_implied_yes, _seg_trust,
                    p_yes_shrunk, _seg_status,
                )
            # Patch posterior so DecisionEngine and all downstream calcs use shrunk p
            posterior = dict(posterior)
            posterior["P_adj_YES"] = p_yes_shrunk
            p_yes = p_yes_shrunk

        tgt_date  = _ticker_date(ticker) or ""
        edge_yes  = (p_yes - market_implied_yes) * 100
        edge_no   = (market_implied_no - p_yes) * 100
        best_edge = max(edge_yes, edge_no)

        # Calibration diagnostic: log every evaluated market (pre-edge-filter)
        _preferred_side = "yes" if edge_yes >= edge_no else "no"
        _strike_z_val = compute_strike_z(ticker, posterior.get("sigma", 4.0))
        _strike_bucket = _strike_distance_bucket(_strike_z_val)
        _h_bucket = _tau_to_bin(posterior.get("tau_hrs", 0.0))
        db.log_calibration_diagnostic(
            ts=datetime.utcnow().isoformat(),
            ticker=ticker,
            city=city_code or None,
            horizon_bucket=_h_bucket,
            strike_distance_bucket=_strike_bucket,
            p_model=float(p_yes),
            p_market=float(yes_ask / 100.0),
            edge=float(best_edge),
            trade_side=None,   # filled in if/when the order submits
            gumbel_mode=Config.GUMBEL_MODE,
            env_mode=env_mode,
            direction=(_semantics.direction if _semantics else None),
            contract_type=(_semantics.contract_type if _semantics else None),
        )

        q_inv    = _slot_net_qty(db, city_code, tgt_date, env_mode) if city_code and tgt_date else 0
        sigma_p  = math.sqrt(max(1.0, posterior.get("posterior_var_T", 1.0))) / 10.0
        as_adj   = q_inv * 0.10 * sigma_p * posterior.get("tau_hrs", 24.0)
        min_edge = max(1.0, MIN_PROB_EDGE_PP + as_adj * 100)

        if best_edge < min_edge:
            diag.n_edge_fail += 1
            continue

        if "KXHIGH" in ticker.upper() and logger.isEnabledFor(logging.DEBUG):
            _var_T = posterior.get("posterior_var_T", 4.0)
            _p_yes = posterior.get("P_adj_YES", 0.5)
            logger.debug(
                "TMAX_CHAIN %s | p_yes=%.4f | var_T=%.3f | sigma_eff=%.4f | "
                "var_source=%s",
                ticker, _p_yes, _var_T,
                0.3 * (max(0.25, _var_T) / 4.0) ** 0.5,   # sigma_eff preview (same formula as logit_jd)
                "tmax_gumbel" if _var_T > 6.0 else "raw_ou",
            )

        posterior["rolling_brier"] = rolling_brier
        # Calibration diagnostic: log P_model vs P_market diff at candidate evaluation
        _p_market = yes_ask / 100.0
        _diff = round((p_yes - _p_market) * 100, 1)
        logger.debug(
            "CALIB %s | P_model=%.3f P_market=%.3f diff=%+.1fc | city=%s tau=%.1fh",
            ticker, p_yes, _p_market, _diff, city_code or "?", posterior.get("tau_hrs", 0),
        )
        intent = engine.evaluate(market, scan_id, current_balance, env_mode, posterior)
        trade_logger.log_decision_step(intent, scan_id, env_mode)

        if not intent:
            diag.n_engine_none += 1
            continue

        # ── Full-spectrum Brier: log all edge-filter+engine passers ───────────
        # Logging here (before gating/risk) removes selection bias from Brier.
        p_brain = posterior.get("P_adj_YES")
        if p_brain is not None:
            tau = posterior.get("tau_hrs", 0.0)
            trade_logger.log_prediction(
                ticker=intent.ticker,
                side=intent.side,
                predicted_p=float(p_brain),
                city=posterior.get("city"),
                tau_hrs=tau,
                horizon_bin=_tau_to_bin(tau),
                sigma=posterior.get("sigma"),
                ar1_correction=posterior.get("ar1_correction"),
            )

        intent.target_qty = min(intent.target_qty, TEST_MAX_QTY)
        # Apply city throttle multiplier (0.5 when Brier is marginal)
        if _size_mult < 1.0:
            intent.target_qty = max(1, int(intent.target_qty * _size_mult))

        # ── 8-gate filter ─────────────────────────────────────────────────────
        spread    = yes_ask + no_ask - 100
        fragility = float(min(yes_ask, no_ask))
        execute_flag, gate_reasons = gating.evaluate(
            side           = intent.side,
            ev_cents       = intent.expected_value * 100.0,
            pi_stale       = posterior.get("pi_stale", 0.5),
            spread         = float(spread),
            fragility      = fragility,
            ess            = posterior.get("pf_ess", 80.0),
            n_particles    = 400,
            depth          = 100,  # no live orderbook depth in REST path → always pass
            tau_settle_hrs = posterior.get("tau_hrs", 99.0),
            posterior_var  = posterior.get("posterior_var_T", 2.0),
        )
        if not execute_flag:
            logger.debug(f"Gate fail {ticker}: {gate_reasons}")
            diag.record_gate_fail(gate_reasons)
            continue

        if not risk_manager.preflight_check(intent, env_mode):
            diag.n_risk_fail += 1
            continue

        ev = getattr(intent, "expected_value", 0.0)
        diag.record_candidate(
            ticker    = ticker,
            city      = city_code or None,
            side      = intent.side,
            p_model   = p_yes,
            p_market  = yes_ask / 100.0,
            tau_hrs   = posterior.get("tau_hrs", 0.0),
            strike_z  = compute_strike_z(ticker, posterior.get("sigma", 4.0)),
        )
        candidates.append((ev, market, posterior, intent, scan_id))

    # ── Phase 2: Safer dedup — up to 2 same-side positions per city+date slot ──
    # Rules: max 2 positions per slot | same side only | min 2°F strike sep |
    #        max 4 total contracts per slot | ranked by EV (best first).
    _MIN_STRIKE_SEP_F    = 2.0
    _MAX_POS_PER_SLOT    = 2
    _MAX_CONTRACTS_SLOT  = 4

    open_pos = db.get_open_positions(env_mode)
    _today = datetime.utcnow().date().isoformat()

    # held_slots: slot_key → list of {side, strike, qty}
    held_slots: Dict[str, List[dict]] = {}
    for pos in open_pos:
        c = _ticker_city(pos["ticker"])
        d = _ticker_date(pos["ticker"])
        if c and d and d >= _today:
            held_slots.setdefault(f"{c}_{d}", []).append({
                "side":   pos["side"],
                "strike": _ticker_strike(pos["ticker"]),
                "qty":    pos["qty"],
            })

    # Sort candidates best-EV first so we always pick the highest-value trade
    candidates.sort(key=lambda x: x[0], reverse=True)

    # cycle_additions: positions decided this cycle (before any submit)
    cycle_additions: Dict[str, List[dict]] = {}
    # best_per_slot: slot_key → list of up to 2 selected candidates
    best_per_slot: Dict[str, List] = {}

    for cand in candidates:
        ev, market, posterior, intent, scan_id = cand
        city     = posterior.get("city") or "UNK"
        tgt_date = _ticker_date(intent.ticker) or "UNK"
        slot_key = f"{city}_{tgt_date}"
        new_side   = intent.side
        new_strike = _ticker_strike(intent.ticker)
        new_qty    = intent.target_qty

        all_in_slot = held_slots.get(slot_key, []) + cycle_additions.get(slot_key, [])
        n_pos      = len(all_in_slot)
        total_qty  = sum(p["qty"] for p in all_in_slot)

        # Gate 1: position cap
        if n_pos >= _MAX_POS_PER_SLOT:
            diag.n_already_held += 1
            continue

        # Gate 2: opposite-side block
        existing_sides = {p["side"] for p in all_in_slot}
        if existing_sides and new_side not in existing_sides:
            logger.debug("BLOCKED_OPPOSITE_SIDE: %s new=%s held=%s", slot_key, new_side, existing_sides)
            diag.n_blocked_opposite_side += 1
            continue

        # Gate 3: strike too close (minimum 2°F separation)
        if new_strike is not None:
            too_close = any(
                p["strike"] is not None and abs(new_strike - p["strike"]) < _MIN_STRIKE_SEP_F
                for p in all_in_slot
            )
            if too_close:
                logger.debug(
                    "BLOCKED_STRIKE_TOO_CLOSE: %s new_strike=%.1f°F",
                    slot_key, new_strike,
                )
                diag.n_blocked_strike_too_close += 1
                continue

        # Gate 4: contract cap
        if total_qty + new_qty > _MAX_CONTRACTS_SLOT:
            logger.debug(
                "BLOCKED_CITY_DATE_CONTRACT_CAP: %s total=%d+%d > %d",
                slot_key, total_qty, new_qty, _MAX_CONTRACTS_SLOT,
            )
            diag.n_blocked_contract_cap += 1
            continue

        # Passed — record candidate
        best_per_slot.setdefault(slot_key, []).append(cand)
        cycle_additions.setdefault(slot_key, []).append({
            "side": new_side, "strike": new_strike, "qty": new_qty,
        })

    _n_final = sum(len(v) for v in best_per_slot.values())
    logger.info(
        "PIPELINE: raw_candidates=%d | blocked_already_held=%d | blocked_opposite_side=%d | "
        "blocked_strike_too_close=%d | blocked_contract_cap=%d | final_executable=%d",
        len(candidates), diag.n_already_held, diag.n_blocked_opposite_side,
        diag.n_blocked_strike_too_close, diag.n_blocked_contract_cap, _n_final,
    )

    # ── Phase 3: Execute winners ──────────────────────────────────────────────
    trades_taken = 0
    for slot_key, slot_cands in best_per_slot.items():
      for ev, market, posterior, intent, scan_id in slot_cands:
        ticker = intent.ticker

        intent_id = trade_logger.log_intent_step(intent, env_mode)

        time_bucket     = int(time.time() / 60)
        client_order_id = str(uuid.uuid5(
            uuid.NAMESPACE_OID,
            f"{ticker}_{intent.side}_{intent.price_cents}_{intent.target_qty}_{time_bucket}"
        ))

        logger.info(
            "INTENT: %s | side=%s | price=%dc | qty=%d | EV=$%.4f | edge_cents=%.2fc | slot=%s",
            ticker, intent.side, intent.price_cents, intent.target_qty,
            intent.expected_value, intent.expected_value * 100.0, slot_key,
        )

        # ── Execution invariant — final belt-and-suspenders before hitting exchange ──
        # 1. EV gate: reject orders whose net EV is below the minimum threshold.
        _edge_cents = intent.expected_value * 100.0
        if _edge_cents < MIN_EV_CENTS:
            logger.warning(
                "BLOCKED_FINAL_EDGE_GUARD: %s edge=%.2fc < threshold=%.0fc — skipping",
                ticker, _edge_cents, MIN_EV_CENTS,
            )
            continue

        # 2. Stale-ask gate: side-specific ask from the most recent scan must not
        #    have drifted past our limit price.  (Live check happens inside executor.)
        _ask_key    = "no_ask" if intent.side == "no" else "yes_ask"
        _cached_ask = market.get(_ask_key, 0)
        if _cached_ask > intent.price_cents:
            logger.warning(
                "BLOCKED_STALE_DRIFT: %s side=%s cached_%s=%dc > limit=%dc — skipping",
                ticker, intent.side, _ask_key, _cached_ask, intent.price_cents,
            )
            continue

        # 3. Cancel cooldown: if this ticker had 3+ canceled/timed-out orders today,
        #    blacklist it for the rest of the trading day to avoid churning thin markets.
        _cancel_count = db.get_canceled_order_count(ticker)
        if _cancel_count >= 3:
            logger.warning(
                "BLOCKED_CANCEL_COOLDOWN: %s — %d canceled attempts today, "
                "blacklisted until next trading day",
                ticker, _cancel_count,
            )
            continue

        # 4. Already-held re-check: guard against a concurrent cycle opening the
        #    same slot between our dedup pass and this submit.
        _fresh_pos = db.get_open_positions(env_mode)
        _fresh_slot_counts: Dict[str, int] = {}
        for _fp in _fresh_pos:
            _fc = _ticker_city(_fp["ticker"])
            _fd = _ticker_date(_fp["ticker"])
            if _fc and _fd:
                _fk = f"{_fc}_{_fd}"
                _fresh_slot_counts[_fk] = _fresh_slot_counts.get(_fk, 0) + 1
        if _fresh_slot_counts.get(slot_key, 0) >= 2:
            logger.warning(
                "BLOCKED_ALREADY_HELD: %s slot=%s has %d positions (cap=2) — skipping",
                ticker, slot_key, _fresh_slot_counts[slot_key],
            )
            continue
        # (Daily gross cap was enforced by risk_manager.preflight_check() above.)

        try:
            order_result = await executor.execute(intent, client_order_id)
        except Exception as e:
            logger.error(f"Execution error for {ticker}: {e}")
            trade_logger.log_order_result(intent, intent_id, "unknown", "ERROR", env_mode)
            continue

        status      = order_result.get("status", "error")
        order_data  = order_result.get("order", {})
        ex_order_id = order_data.get("order_id", "unknown")

        order_db_id = trade_logger.log_order_result(intent, intent_id, ex_order_id, status, env_mode)

        # Log fill when the inner order is confirmed executed — regardless of the
        # outer result status, which varies by Kalshi API response shape.
        if order_data.get("status") == "executed" and order_db_id is not None:
            _side_price_key = "yes_price" if intent.side == "yes" else "no_price"
            fill_price = (
                order_data.get(_side_price_key)
                or order_data.get("price")
                or intent.price_cents
            )
            fill_price = int(fill_price)
            yes_ask = market.get("yes_ask", 50)
            no_ask  = market.get("no_ask", 50)
            scan_mid = (yes_ask + (100 - no_ask)) // 2
            fees_c = float(_kalshi_fee_per_contract(fill_price) * intent.target_qty)
            _fill_sem = posterior.get("semantics")
            trade_logger.log_execution_fill(
                order_id=order_db_id,
                exchange_trade_id=ex_order_id,
                ticker=intent.ticker,
                side=intent.side,
                price=fill_price,
                qty=intent.target_qty,
                env_mode=env_mode,
                scan_mid_cents=scan_mid,
                predicted_p=posterior.get("P_adj_YES"),
                market_implied_p=yes_ask / 100.0,
                city=posterior.get("city"),
                horizon_bin=_tau_to_bin(posterior.get("tau_hrs", 0.0)),
                expected_value_cents=intent.expected_value * 100.0,
                fees_cents=fees_c,
                direction=(_fill_sem.direction if _fill_sem else None),
                contract_type=(_fill_sem.contract_type if _fill_sem else None),
            )
            logger.info(f"FILLED: {ticker} @ {fill_price}c x {intent.target_qty}")
            trades_taken += 1
            diag.n_fills += 1
            diag.mark_filled(ticker)

    _n_final = sum(len(v) for v in best_per_slot.values())
    logger.info(
        "Trade Cycle Complete. Filled: %d | Unique slots: %d | Orders attempted: %d",
        trades_taken, len(best_per_slot), _n_final,
    )
    report = diag.generate_report(db)
    logger.info(report)

    # Bias detection: check rolling 7-day P_model - P_market per city
    _cities_seen = {c.city for c in diag.candidates if c.city}
    for _city in sorted(_cities_seen):
        _avg_edge, _n = db.get_city_edge_summary(_city, n_days=7, min_n=20)
        if _n >= 20:
            if _avg_edge < -0.05:
                logger.warning(
                    "MODEL_NO_BIAS: %s avg_edge=%.3f n=%d (last 7d) — model systematically short",
                    _city, _avg_edge, _n,
                )
            elif _avg_edge > 0.05:
                logger.warning(
                    "MODEL_YES_BIAS: %s avg_edge=%.3f n=%d (last 7d) — model systematically long",
                    _city, _avg_edge, _n,
                )

    # Experiment run log: one summary row per cycle date+gumbel_mode
    try:
        _run_date = datetime.utcnow().strftime("%Y-%m-%d")
        _yes_fills = sum(1 for c in diag.candidates if c.filled and c.side == "yes")
        _no_fills  = sum(1 for c in diag.candidates if c.filled and c.side == "no")
        _rolling_brier = db.get_rolling_brier(n_days=7)
        db.upsert_experiment_run(
            run_date=_run_date,
            gumbel_mode=Config.GUMBEL_MODE,
            total_trades=diag.n_fills,
            yes_trades=_yes_fills,
            no_trades=_no_fills,
            avg_edge_cents=None,   # populated by calibration_report.py offline
            avg_lvr_cents=None,
            realized_pnl_cents=None,
            brier_score=_rolling_brier,
            n_settled=0,           # populated by check_outcomes.py
        )
    except Exception as _e:
        logger.error("EXPERIMENT_RUN_LOG_FAILED: %s", _e)

    # Portfolio reconcile: pull recent fills from Kalshi and write any that
    # weren't captured in real-time (IOC orders that filled before our poll).
    try:
        fills = await client.get_portfolio_fills(limit=100)
        with db.get_connection() as _conn:
            _c = _conn.cursor()
            _c.execute("SELECT order_id, exchange_order_id, ticker, side, environment FROM orders WHERE exchange_order_id IS NOT NULL")
            _order_map = {r[1]: (r[0], r[2], r[3], r[4]) for r in _c.fetchall()}
            _c.execute("SELECT exchange_trade_id FROM executions")
            _seen = {r[0] for r in _c.fetchall()}
        _reconciled = 0
        for _f in fills:
            _fid = _f["fill_id"]
            if _fid in _seen or _f["order_id"] not in _order_map:
                continue
            _oid, _ticker, _side, _env = _order_map[_f["order_id"]]
            _qty   = int(float(_f["count_fp"]))
            _price = int(round(float(_f["yes_price_dollars"]) * 100)) if _side == "yes" else int(round(float(_f["no_price_dollars"]) * 100))
            _ts    = _f["created_time"].replace("Z", "")
            _exec_id = db.log_execution(order_id=_oid, exchange_trade_id=_fid, ticker=_ticker, side=_side, price=_price, qty=_qty, environment=_env, timestamp=_ts)
            if _exec_id:
                with db.get_connection() as _conn:
                    _c = _conn.cursor()
                    _c.execute("UPDATE orders SET status='executed', updated_at=GETDATE() WHERE order_id=?", (_oid,))
                    _conn.commit()
                db.log_trade_attribution(
                    ticker=_ticker,
                    execution_id=_exec_id,
                    city=_ticker_city(_ticker),
                    side=_side,
                    fill_price_cents=_price,
                )
                _reconciled += 1
        if _reconciled:
            logger.info("FILL_RECONCILE: captured %d missed fill(s) from Kalshi portfolio", _reconciled)
    except Exception as _e:
        logger.error("FILL_RECONCILE failed: %s", _e)


async def monitor_positions(env_mode: str) -> int:
    """
    Scan all open positions and exit any that hit a profit/loss threshold.

    Exit triggers (evaluated in priority order):
      1. PROFIT_TARGET  — unrealized gain >= EXIT_PROFIT_TARGET (30%)
      2. EXPIRY_CLEANUP — any profit and < EXIT_EXPIRY_CLEANUP_TAU_HRS to settlement
      3. STOP_LOSS      — losing >= |EXIT_STOP_LOSS| (50%) and < EXIT_STOP_LOSS_TAU_MAX_HRS left

    Returns the number of positions exited this call.
    """
    db       = DWTraderDB()
    executor = ExecutionManager(client)

    positions = db.get_open_positions(env_mode)
    if not positions:
        return 0

    logger.info("Position monitor: checking %d open position(s) [%s]", len(positions), env_mode)
    exits = 0

    for pos in positions:
        ticker   = pos["ticker"]
        side     = pos["side"]
        qty      = pos["qty"]
        avg_cost = pos["avg_price_cents"]  # integer cents
        pos_id   = pos["position_id"]

        # ── Fetch live market snapshot ─────────────────────────────────────────
        market = await client.get_market(ticker)
        if not market:
            logger.debug("monitor: no market data for %s — skipping", ticker)
            continue

        # If Kalshi says the market is closed/settled, write realized P&L and mark settled
        if market.get("status") in ("settled", "closed", "finalized"):
            result_side = market.get("result")
            if result_side in ("yes", "no"):
                db.settle_position_with_outcome(ticker, yes_won=(result_side == "yes"))
            else:
                db.mark_position_settled(ticker)
            continue

        # Current bid for our side = best price someone will buy our contracts at
        bid_key     = f"{side}_bid"
        current_bid = market.get(bid_key)
        if not current_bid or current_bid <= 0:
            logger.debug("monitor: no %s for %s — skipping", bid_key, ticker)
            continue

        # ── Update unrealized PnL in DB ────────────────────────────────────────
        unrealized = (current_bid - avg_cost) * qty
        db.update_position_pnl(pos_id, unrealized)

        # ── Time to settlement ─────────────────────────────────────────────────
        tau_hrs = 99.0
        close_time = market.get("close_time") or market.get("expiration_time")
        if close_time:
            try:
                dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                tau_hrs = max(0.0, (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0)
            except Exception:
                pass

        # Market has already expired — let Kalshi settle it; don't try to sell
        if tau_hrs == 0.0:
            db.mark_position_settled(ticker)
            continue

        pnl_pct = (current_bid - avg_cost) / avg_cost if avg_cost > 0 else 0.0

        # ── Exit decision ──────────────────────────────────────────────────────
        exit_reason: Optional[str] = None

        if pnl_pct >= EXIT_PROFIT_TARGET:
            exit_reason = f"PROFIT_TARGET pnl={pnl_pct:+.1%} bid={current_bid}c"
        elif tau_hrs < EXIT_EXPIRY_CLEANUP_TAU_HRS and pnl_pct > 0:
            exit_reason = f"EXPIRY_CLEANUP pnl={pnl_pct:+.1%} tau={tau_hrs:.1f}h"
        elif tau_hrs < EXIT_STOP_LOSS_TAU_MAX_HRS and pnl_pct <= EXIT_STOP_LOSS:
            exit_reason = f"STOP_LOSS pnl={pnl_pct:+.1%} tau={tau_hrs:.1f}h"

        if exit_reason is None:
            logger.debug(
                "monitor: %s %s avg=%.0fc bid=%.0fc pnl=%+.1%% tau=%.1fh — hold",
                ticker, side, avg_cost, current_bid, pnl_pct, tau_hrs,
            )
            continue

        # Cross the spread by 2 cents so the IOC sell order fills immediately
        # instead of resting and getting canceled. The fill will execute at the
        # actual best bid, not at sell_price — the limit just prevents fills
        # below sell_price. 2c = ~2-3% slippage on a typical 70-80c position.
        sell_price = max(1, current_bid - 2)

        logger.info(
            "EXIT SIGNAL: %s %s qty=%d avg=%.0fc bid=%.0fc sell=%.0fc | %s",
            ticker, side, qty, avg_cost, current_bid, sell_price, exit_reason,
        )

        # ── Idempotency guard: re-read qty before submitting ──────────────────
        # Prevents duplicate sells if monitor fires twice while a sell is in-flight.
        fresh = db.get_open_positions(env_mode)
        live_pos = next((p for p in fresh if p["position_id"] == pos_id), None)
        if not live_pos or live_pos["qty"] <= 0:
            logger.info("Position %s already closed — skipping duplicate exit", ticker)
            continue

        # ── Submit sell order ──────────────────────────────────────────────────
        client_order_id = str(uuid.uuid5(
            uuid.NAMESPACE_OID,
            f"CLOSE_{ticker}_{side}_{int(time.time() / 60)}",
        ))

        try:
            result = await executor.close_position(
                ticker=ticker,
                side=side,
                qty=qty,
                bid_cents=sell_price,
                client_order_id=client_order_id,
            )
        except Exception as exc:
            logger.error("close_position raised for %s: %s", ticker, exc)
            continue

        status     = result.get("status", "error")
        order_data = result.get("order", {})

        if status != "submitted" or order_data.get("status") != "executed":
            logger.warning(
                "Exit order for %s returned status=%s order_status=%s",
                ticker, status, order_data.get("status"),
            )
            continue

        # Kalshi order response stores the limit price as yes_price / no_price.
        # Fall back to sell_price (the submitted limit) if the key is absent.
        fill_price   = order_data.get(f"{side}_price") or order_data.get("price") or sell_price
        realized_pnl = (fill_price - avg_cost) * qty

        # ── Log sell order + execution + position close ────────────────────────
        ex_order_id = order_data.get("order_id", "unknown")
        order_db_id = db.log_order(
            intent_id=None,
            exchange_order_id=ex_order_id,
            ticker=ticker,
            side=side,
            price=fill_price,
            qty=qty,
            order_type="sell",
            status="submitted",
            environment=env_mode,
            gumbel_mode=Config.GUMBEL_MODE,
        )
        if order_db_id:
            db.log_execution_record(
                order_id=order_db_id,
                exchange_trade_id=ex_order_id,
                price_cents=fill_price,
                qty=qty,
                environment=env_mode,
            )
        db.log_position_close(
            position_id=pos_id,
            fill_price_cents=fill_price,
            qty_sold=qty,
            realized_pnl_cents=realized_pnl,
            exit_reason=exit_reason,
        )

        logger.info(
            "EXITED: %s %s %d contracts @ %dc | realized=%.0fc (%+.1%%)",
            ticker, side, qty, fill_price, realized_pnl, pnl_pct,
        )
        exits += 1

    if exits:
        logger.info("Position monitor complete: %d exit(s) executed", exits)
    return exits


async def main():
    logger.info("Kalshi Weather Brain Bot — Demo Mode")

    balance = await client.get_balance()
    logger.info(f"Demo balance: ${balance:.2f}")

    env_mode = getattr(Config, "ENV_EXECUTION_MODE", "PAPER")
    if hasattr(env_mode, "value"):
        env_mode = env_mode.value

    await trade_cycle(env_mode)


if __name__ == "__main__":
    asyncio.run(main())
