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

from src.decision.engine import DecisionEngine
from src.risk.manager import RiskManager
from src.execution.manager import ExecutionManager
from src.logging.trade_logger import TradeLogger
from src.brain.logit_jd import LogitJumpDiffusionBrain
import numpy as np
from src.layer2.particle_filter import TemperatureParticleFilter
from src.layer2.gating_logic import TradeGating
from src.brain.weather_estimator import (
    estimate_p_yes, get_ar1_metadata, get_forecast_temp_for_ticker,
    load_city_params, _CITY_MAP, _FORECAST_SIGMA_F, _AR1_PHI
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] - %(message)s')
logger = logging.getLogger("IndexOrchestrator")

# Max contracts per order during testing
TEST_MAX_QTY = 2

# Minimum edge: our fair-value P(YES) must differ from the market-implied P(YES)
# by at least this many cents before we consider the opportunity worth trading.
# 15c = 15 percentage points of edge required.
MIN_EDGE_CENTS = 15

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
    """KXHIGHCHI-26APR21-T73 → 'CHI'"""
    m = _re.match(r"KX(?:HIGH|TEMP)([A-Z]+)-", ticker, _re.IGNORECASE)
    return m.group(1).upper() if m else None


def _ticker_date(ticker: str) -> Optional[str]:
    """KXHIGHCHI-26APR21-T73 → '2026-04-21'"""
    m = _re.match(r"KX(?:HIGH|TEMP)[A-Z]+-(\d{2}[A-Z]{3}\d{2})", ticker, _re.IGNORECASE)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).upper(), "%y%b%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _tau_to_bin(tau_hrs: float) -> str:
    if tau_hrs < 6:   return "0-6h"
    if tau_hrs < 12:  return "6-12h"
    if tau_hrs < 24:  return "12-24h"
    if tau_hrs < 48:  return "24-48h"
    return "48h+"


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
) -> Dict[str, Any]:
    """
    Build a posterior dict for the brain.
    Tries to get an independent weather-based P(YES) from Open-Meteo.
    Falls back to high-uncertainty defaults when the ticker is unrecognised.

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
    city   = _ticker_city(ticker)

    # Per-city calibrated params (defaults when < 14 days of residuals)
    cparams    = (city_params or {}).get(city or "", {})
    sigma_city = cparams.get("sigma", _FORECAST_SIGMA_F)
    phi_city   = cparams.get("phi",   _AR1_PHI)

    # Independent weather estimate from Open-Meteo using per-city σ and φ
    p_adj = await estimate_p_yes(ticker, sigma_f=sigma_city, phi=phi_city)

    # Staleness: high when no weather estimate available or near expiry
    pi_stale = 0.1 if p_adj is not None else 0.4

    # AR(1) correction: recompute with per-city φ from cached e_prev
    ar1_correction = 0.0
    if city and city in _CITY_MAP:
        lat, lon, _ = _CITY_MAP[city]
        meta = get_ar1_metadata(lat, lon)
        e_prev = meta.get("e_prev", 0.0) if meta else 0.0
        ar1_correction = phi_city * e_prev

    # Run SMC particle filter to get horizon-scaled temperature uncertainty.
    # Uses OU propagation: short-horizon markets → tight var, long-horizon → wide var.
    # sigma_city / 2 maps forecast σ (4°F) to a sensible particle init spread (2°F default).
    posterior_var_T, pf_ess = _run_pf_variance(
        ticker=ticker,
        ar1_correction=ar1_correction,
        tau_hrs=tau_hrs,
        sigma_city=sigma_city / 2.0,
    )

    return {
        "P_adj_YES":       p_adj,
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
        if _ticker_city(r["ticker"]) == city and _ticker_date(r["ticker"]) == settle_date:
            net += r["qty"] if r["side"] == "yes" else -r["qty"]
    return net


async def trade_cycle(env_mode: str):
    logger.info(f"Starting Trade Cycle [{env_mode}]")

    db           = DWTraderDB()
    brain        = LogitJumpDiffusionBrain(sigma_belief=0.3, kappa_mkt=0.3, max_alpha_mkt=0.40)
    engine       = DecisionEngine(brain=brain, max_kelly_fraction=0.15, kelly_multiplier=0.25, min_total_ev=3.0)
    risk_manager = RiskManager(db)
    executor     = ExecutionManager(client)
    trade_logger = TradeLogger(db)
    gating       = TradeGating()

    current_balance = Config.BANKROLL

    markets = await client.get_weather_markets()
    if not markets:
        logger.warning("No weather markets returned. Sleeping.")
        return

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
        valid_markets.append(market)

    # ── Phase 1b: Build posteriors in parallel ────────────────────────────────
    # All _build_posterior() calls fire simultaneously so Open-Meteo fetches for
    # all ~19 unique cities happen concurrently instead of sequentially.
    # The module-level _forecast_cache and _ar1_error_cache ensure each unique
    # city+date is only fetched once despite concurrent access.
    posteriors_raw = await asyncio.gather(
        *[_build_posterior(m, city_params) for m in valid_markets],
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
            continue

        p_yes = posterior.get("P_adj_YES")
        if p_yes is None:
            continue

        # ── Avellaneda-Stoikov inventory-aware edge threshold ──────────────────
        # Base threshold (15c) is bumped upward when we already hold exposure in
        # the same city/date slot, reducing willingness to add correlated risk.
        city_code = posterior.get("city") or ""
        tgt_date  = _ticker_date(ticker) or ""
        market_implied_yes = yes_ask / 100.0
        market_implied_no  = 1.0 - no_ask / 100.0
        edge_yes  = (p_yes - market_implied_yes) * 100
        edge_no   = (market_implied_no - p_yes) * 100
        best_edge = max(edge_yes, edge_no)

        q_inv    = _slot_net_qty(db, city_code, tgt_date, env_mode) if city_code and tgt_date else 0
        sigma_p  = math.sqrt(max(1.0, posterior.get("posterior_var_T", 1.0))) / 10.0
        as_adj   = q_inv * 0.10 * sigma_p * posterior.get("tau_hrs", 24.0)
        min_edge = max(1.0, MIN_EDGE_CENTS + as_adj * 100)

        if best_edge < min_edge:
            continue

        intent = engine.evaluate(market, scan_id, current_balance, env_mode, posterior)
        trade_logger.log_decision_step(intent, scan_id, env_mode)

        if not intent:
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
            continue

        if not risk_manager.preflight_check(intent, env_mode):
            continue

        ev = getattr(intent, "expected_value", 0.0)
        candidates.append((ev, market, posterior, intent, scan_id))

    # ── Phase 2: Deduplicate — keep best-EV per city+date ────────────────────
    # One position per city per settlement date eliminates correlated bets
    # and contradictory exposures (e.g., YES on T71 + NO on B70.5, same city/day).
    # Also exclude slots we already hold in the DB — prevents re-entry across cycles.
    open_pos = db.get_open_positions(env_mode)
    already_held: set = set()
    for pos in open_pos:
        c = _ticker_city(pos["ticker"])
        d = _ticker_date(pos["ticker"])
        if c and d:
            already_held.add(f"{c}_{d}")

    best_per_slot: Dict[str, Candidate] = {}
    for cand in candidates:
        ev, market, posterior, intent, scan_id = cand
        city      = posterior.get("city") or "UNK"
        tgt_date  = _ticker_date(intent.ticker) or "UNK"
        slot_key  = f"{city}_{tgt_date}"
        if slot_key in already_held:
            continue
        if slot_key not in best_per_slot or ev > best_per_slot[slot_key][0]:
            best_per_slot[slot_key] = cand

    logger.info(
        f"Candidates: {len(candidates)} | After city+date dedup: {len(best_per_slot)} unique slots"
        f" | Already held: {len(already_held)} slots skipped"
    )

    # ── Phase 3: Execute winners ──────────────────────────────────────────────
    trades_taken = 0
    for slot_key, (ev, market, posterior, intent, scan_id) in best_per_slot.items():
        ticker = intent.ticker

        intent_id = trade_logger.log_intent_step(intent, env_mode)

        time_bucket     = int(time.time() / 60)
        client_order_id = str(uuid.uuid5(
            uuid.NAMESPACE_OID,
            f"{ticker}_{intent.side}_{intent.price_cents}_{intent.target_qty}_{time_bucket}"
        ))

        logger.info(
            f"INTENT: {ticker} | side={intent.side} | price={intent.price_cents}c "
            f"| qty={intent.target_qty} | EV={intent.expected_value:.4f} "
            f"| edge={ev:.2f}c | slot={slot_key}"
        )

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

        if status == "submitted" and order_data.get("status") == "executed":
            fill_price = order_data.get("yes_price", intent.price_cents)
            trade_logger.log_execution_fill(
                order_id=order_db_id,
                exchange_trade_id=ex_order_id,
                ticker=intent.ticker,
                side=intent.side,
                price=fill_price,
                qty=intent.target_qty,
                env_mode=env_mode,
            )
            logger.info(f"FILLED: {ticker} @ {fill_price}c x {intent.target_qty}")
            trades_taken += 1

    logger.info(f"Trade Cycle Complete. Filled: {trades_taken} | Unique slots targeted: {len(best_per_slot)}")


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

        # Skip already-settled markets (settlement handled by check_outcomes.py)
        if market.get("status") in ("settled", "closed", "finalized"):
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
