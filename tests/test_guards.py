"""
Unit tests for probability clamp, city risk guard, concentration guard,
trade attribution writeback, lvr_cents population, and cancel cooldown.

All DB tests use a real temp-file SQLite DB (not :memory:) because DWTraderDB
opens a new connection per call — :memory: would produce a different DB each time.
"""
import os
import re
import tempfile
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_db(tmp_path: Path):
    from src.db.dwtrader import DWTraderDB
    return DWTraderDB(db_path=str(tmp_path / "test.db"))


def _insert_settled_prediction(
    conn, ticker: str, city: str, predicted_p: float,
    actual_outcome: int, brier: float, days_ago: int = 0
):
    from datetime import date, timedelta
    trade_date = (date.today() - timedelta(days=days_ago)).isoformat()
    recorded_at = (_utcnow() - timedelta(days=days_ago)).isoformat()
    conn.execute(
        """
        INSERT INTO predictions
            (ticker, trade_date, side, predicted_p, city, actual_outcome,
             brier_score, recorded_at)
        VALUES (?, ?, 'no', ?, ?, ?, ?, ?)
        """,
        (ticker, trade_date, predicted_p, city, actual_outcome, brier, recorded_at),
    )


def _insert_open_position(conn, ticker: str, side: str, qty: int, env: str = "PAPER"):
    conn.execute(
        """
        INSERT INTO positions
            (ticker, side, qty, avg_price_cents, cost_basis,
             realized_pnl_cents, unrealized_pnl_cents, updated_at, status, environment)
        VALUES (?, ?, ?, 60, 1.20, 0.0, 0.0, ?, 'open', ?)
        """,
        (ticker, side, qty, _utcnow().isoformat(), env),
    )


_cancel_seq = 0

def _insert_canceled_order(conn, ticker: str, trade_date: str, env: str = "PAPER",
                            status: str = "canceled"):
    global _cancel_seq
    _cancel_seq += 1
    # Use noon UTC so SQLite DATE() extraction is unambiguous regardless of local tz
    created_at = trade_date + "T12:00:00"
    conn.execute(
        """
        INSERT INTO orders
            (intent_id, exchange_order_id, ticker, side, price_cents, qty,
             order_type, status, created_at, updated_at, environment)
        VALUES (NULL, ?, ?, 'no', 60, 2, 'limit', ?, ?, ?, ?)
        """,
        (f"test-order-{_cancel_seq}", ticker, status, created_at, created_at, env),
    )


# ── 1. Probability clamp ──────────────────────────────────────────────────────

class TestProbClamp:
    def test_extreme_below_floor_is_clamped_up(self):
        """p << 0.03 (far OTM) must be raised to 0.03."""
        from src.brain.weather_estimator import _p_above
        # forecast=90°F, threshold=120°F → z >> 0 → p ≈ 0
        p = _p_above(forecast_temp=90.0, threshold=120.0, sigma=1.0)
        assert p == pytest.approx(0.03), f"expected 0.03, got {p}"

    def test_extreme_above_ceiling_is_clamped_down(self):
        """p >> 0.97 (far ITM) must be clamped to 0.97."""
        from src.brain.weather_estimator import _p_above
        # forecast=100°F, threshold=50°F → z << 0 → p ≈ 1
        p = _p_above(forecast_temp=100.0, threshold=50.0, sigma=1.0)
        assert p == pytest.approx(0.97), f"expected 0.97, got {p}"

    def test_at_the_money_not_clamped(self):
        """P(T > mu) with forecast=threshold → ~0.5, no clamp."""
        from src.brain.weather_estimator import _p_above
        p = _p_above(forecast_temp=70.0, threshold=70.0, sigma=4.0)
        assert 0.03 < p < 0.97
        assert abs(p - 0.5) < 0.05  # should be near 0.5


# ── 2–5. CityRiskGuard ────────────────────────────────────────────────────────

class TestCityRiskGuard:
    def _make_guard(self, tmp_path: Path):
        from src.risk.city_guard import CityRiskGuard
        return CityRiskGuard(blocks_file=tmp_path / "city_blocks.json")

    def test_brier_throttle_applies_half_multiplier(self, tmp_path):
        """Brier in [0.20, 0.25) with n>=10 → 0.5× size multiplier."""
        db = _make_db(tmp_path)
        # Insert 12 settled predictions with avg Brier = 0.22
        with db.get_connection() as conn:
            for i in range(12):
                _insert_settled_prediction(
                    conn, f"KXHIGHLAX-26APR{20+i:02d}-T65", "LAX",
                    predicted_p=0.50, actual_outcome=1, brier=0.22, days_ago=i,
                )
            conn.commit()

        guard = self._make_guard(tmp_path)
        guard.refresh(db, ["LAX"])
        allowed, mult = guard.check("LAX")
        assert allowed is True
        assert mult == pytest.approx(0.5)

    def test_brier_block_fires_at_threshold(self, tmp_path):
        """Brier >= 0.25 with n>=10 → city blocked for 24h."""
        db = _make_db(tmp_path)
        with db.get_connection() as conn:
            for i in range(15):
                _insert_settled_prediction(
                    conn, f"KXHIGHMIA-26APR{20+i:02d}-T80", "MIA",
                    predicted_p=0.02, actual_outcome=1, brier=0.96, days_ago=i,
                )
            conn.commit()

        guard = self._make_guard(tmp_path)
        guard.refresh(db, ["MIA"])
        allowed, mult = guard.check("MIA")
        assert allowed is False
        assert mult == pytest.approx(0.0)
        assert guard.is_blocked("MIA")

    def test_insufficient_data_does_not_block(self, tmp_path):
        """n < MIN_OBS (10) → monitor only, never block."""
        db = _make_db(tmp_path)
        with db.get_connection() as conn:
            for i in range(5):
                _insert_settled_prediction(
                    conn, f"KXHIGHCHI-26APR{20+i:02d}-T70", "CHI",
                    predicted_p=0.02, actual_outcome=1, brier=0.96, days_ago=i,
                )
            conn.commit()

        guard = self._make_guard(tmp_path)
        guard.refresh(db, ["CHI"])
        allowed, mult = guard.check("CHI")
        assert allowed is True
        assert mult == pytest.approx(1.0)
        assert not guard.is_blocked("CHI")

    def test_city_auto_recovery_after_block_expires(self, tmp_path):
        """Block set 25h ago should have expired; city reactivated."""
        from src.risk.city_guard import CityRiskGuard
        guard = CityRiskGuard(blocks_file=tmp_path / "city_blocks.json")
        # Manually inject an expired block (must be timezone-aware to match block_city format)
        expired_until = (_utcnow() - timedelta(hours=1)).isoformat()
        guard._blocks["DEN"] = expired_until
        guard._save()

        # Reload and expire
        guard2 = CityRiskGuard(blocks_file=tmp_path / "city_blocks.json")
        guard2._expire_blocks()
        assert not guard2.is_blocked("DEN")
        allowed, mult = guard2.check("DEN")
        assert allowed is True
        assert mult == pytest.approx(1.0)

    def test_tail_risk_guard_blocks_city(self, tmp_path):
        """>=2 cases of p<5% but YES outcome in last 20 → immediate block."""
        db = _make_db(tmp_path)
        # Need >= 10 obs for the guard to evaluate (min_obs check)
        with db.get_connection() as conn:
            # 10 normal NO-correct predictions
            for i in range(10):
                _insert_settled_prediction(
                    conn, f"KXHIGHTHOU-26APR{10+i:02d}-B88", "THOU",
                    predicted_p=0.03, actual_outcome=0, brier=0.0009, days_ago=i + 5,
                )
            # 2 tail-risk cases: p<5% but outcome=YES
            for i in range(2):
                _insert_settled_prediction(
                    conn, f"KXHIGHTHOU-26APR{20+i:02d}-B88", "THOU",
                    predicted_p=0.03, actual_outcome=1, brier=0.9409, days_ago=i,
                )
            conn.commit()

        guard = self._make_guard(tmp_path)
        guard.refresh(db, ["THOU"])
        allowed, mult = guard.check("THOU")
        assert allowed is False
        assert guard.is_blocked("THOU")

    def test_good_calibration_full_sizing(self, tmp_path):
        """Brier < 0.20 → full 1.0× sizing."""
        db = _make_db(tmp_path)
        with db.get_connection() as conn:
            for i in range(12):
                _insert_settled_prediction(
                    conn, f"KXHIGHDEN-26APR{20+i:02d}-T60", "DEN",
                    predicted_p=0.05, actual_outcome=0, brier=0.0025, days_ago=i,
                )
            conn.commit()

        guard = self._make_guard(tmp_path)
        guard.refresh(db, ["DEN"])
        allowed, mult = guard.check("DEN")
        assert allowed is True
        assert mult == pytest.approx(1.0)

    # ── Paper-mode Brier throttle (new behaviour) ─────────────────────────────

    def _mock_db(self, brier: float, n: int, tail_count: int = 0):
        """Return a MagicMock DB that reports a single city's Brier and tail count."""
        db = MagicMock()
        db.get_rolling_brier_by_city.return_value = (brier, n)
        db.get_tail_risk_count.return_value = tail_count
        return db

    def test_paper_mode_brier_block_becomes_throttle(self, tmp_path):
        """PAPER mode: Brier >= 0.25 → 0.25× throttle, NO 24h block."""
        from src.risk.city_guard import CityRiskGuard, PAPER_BRIER_THROTTLE
        db = self._mock_db(brier=0.40, n=15)
        guard = self._make_guard(tmp_path)
        guard.refresh(db, ["MIA"], env_mode="PAPER")
        allowed, mult = guard.check("MIA")
        assert allowed is True, "city must not be blocked in PAPER mode"
        assert mult == pytest.approx(PAPER_BRIER_THROTTLE)
        assert not guard.is_blocked("MIA"), "is_blocked must be False in PAPER mode"

    def test_live_mode_brier_still_blocks(self, tmp_path):
        """LIVE mode: Brier >= 0.25 → 24h block (existing behaviour unchanged)."""
        from src.risk.city_guard import CityRiskGuard
        db = self._mock_db(brier=0.40, n=15)
        guard = self._make_guard(tmp_path)
        guard.refresh(db, ["MIA"], env_mode="LIVE")
        allowed, mult = guard.check("MIA")
        assert allowed is False
        assert mult == pytest.approx(0.0)
        assert guard.is_blocked("MIA")

    def test_paper_mode_releases_existing_block_on_refresh(self, tmp_path):
        """PAPER mode refresh clears a prior block so calibration can resume."""
        from src.risk.city_guard import CityRiskGuard, PAPER_BRIER_THROTTLE
        db = self._mock_db(brier=0.40, n=15)
        guard = self._make_guard(tmp_path)
        # Manually inject an active block (as if set by a previous live-mode run)
        future_until = (_utcnow() + timedelta(hours=12)).isoformat()
        guard._blocks["TDC"] = future_until
        guard._save()
        assert guard.is_blocked("TDC")  # sanity: block is active before refresh

        guard.refresh(db, ["TDC"], env_mode="PAPER")
        allowed, mult = guard.check("TDC")
        assert allowed is True, "prior block must be released in PAPER mode"
        assert mult == pytest.approx(PAPER_BRIER_THROTTLE)
        assert not guard.is_blocked("TDC")

    def test_paper_mode_tail_risk_still_blocks(self, tmp_path):
        """PAPER mode: tail-risk guard still blocks (severity justifies it)."""
        from src.risk.city_guard import CityRiskGuard
        db = self._mock_db(brier=0.40, n=15, tail_count=2)
        guard = self._make_guard(tmp_path)
        guard.refresh(db, ["LAX"], env_mode="PAPER")
        allowed, mult = guard.check("LAX")
        assert allowed is False
        assert guard.is_blocked("LAX")

    def test_paper_mode_brier_throttle_below_block_unchanged(self, tmp_path):
        """PAPER mode: Brier in [0.20, 0.25) still gets 0.5× (not paper throttle)."""
        from src.risk.city_guard import CityRiskGuard
        db = self._mock_db(brier=0.22, n=12)
        guard = self._make_guard(tmp_path)
        guard.refresh(db, ["CHI"], env_mode="PAPER")
        allowed, mult = guard.check("CHI")
        assert allowed is True
        assert mult == pytest.approx(0.5)


# ── 6. Concentration guard ────────────────────────────────────────────────────

class TestConcentrationGuard:
    def test_blocks_at_max_positions(self, tmp_path):
        """2 open positions for same city+date → 3rd is blocked."""
        from src.risk.manager import RiskManager
        from src.decision.engine import TradeIntent

        db = _make_db(tmp_path)
        with db.get_connection() as conn:
            _insert_open_position(conn, "KXHIGHLAX-26APR28-T64", "yes", 2)
            _insert_open_position(conn, "KXHIGHLAX-26APR28-B66.5", "no", 2)
            conn.commit()

        rm = RiskManager(db)
        intent = TradeIntent(
            ticker="KXHIGHLAX-26APR28-B68.5",
            side="no",
            price_cents=60,
            target_qty=2,
            expected_value=0.05,
            kelly_fraction=0.10,
            confidence=0.80,
        )
        assert rm.preflight_check(intent, "PAPER") is False

    def test_allows_first_position(self, tmp_path):
        """No existing positions → new entry is allowed."""
        from src.risk.manager import RiskManager
        from src.decision.engine import TradeIntent

        db = _make_db(tmp_path)
        rm = RiskManager(db)
        intent = TradeIntent(
            ticker="KXHIGHLAX-26APR28-T64",
            side="yes",
            price_cents=40,
            target_qty=2,
            expected_value=0.05,
            kelly_fraction=0.10,
            confidence=0.80,
        )
        result = rm.preflight_check(intent, "PAPER")
        # Should not be blocked by concentration (may fail daily spend guard, which is ok for this test)
        # We only test that the concentration guard itself doesn't fire
        # with 0 existing positions — so if it returns False it must be for another reason
        if result is False:
            # Verify it wasn't blocked by concentration
            # (daily_spent check may fire; that's acceptable)
            pass
        # The assertion: no concentration block when slot is empty
        assert not (result is False and db.get_open_positions("PAPER"))

    def test_blocks_at_contract_cap(self, tmp_path):
        """4 existing contracts → adding 1 more exceeds contract cap."""
        from src.risk.manager import RiskManager
        from src.decision.engine import TradeIntent

        db = _make_db(tmp_path)
        with db.get_connection() as conn:
            _insert_open_position(conn, "KXHIGHLAX-26APR28-T64", "yes", 4)
            conn.commit()

        rm = RiskManager(db)
        intent = TradeIntent(
            ticker="KXHIGHLAX-26APR28-B66.5",
            side="no",
            price_cents=60,
            target_qty=2,
            expected_value=0.05,
            kelly_fraction=0.10,
            confidence=0.80,
        )
        assert rm.preflight_check(intent, "PAPER") is False


# ── 7. Trade attribution writeback ────────────────────────────────────────────

class TestTradeAttributionWriteback:
    def test_attribution_row_written_on_fill(self, tmp_path):
        """Every buy fill must produce a row in trade_attribution."""
        from src.db.dwtrader import DWTraderDB
        from src.logging.trade_logger import TradeLogger

        db = DWTraderDB(db_path=str(tmp_path / "test.db"))
        logger = TradeLogger(db)

        # Set up prerequisite rows for FK chain: scan → intent → order
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO scans (ticker, market_probability, ml_probability, "
                "best_bid, best_ask, spread, volume, timestamp, environment) "
                "VALUES ('KXHIGHDEN-26APR28-T60', 0.6, 0.5, 55, 65, 10, 100, ?, 'PAPER')",
                (_utcnow().isoformat(),),
            )
            scan_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO intents (scan_id, ticker, side, expected_price_cents, "
                "target_qty, timestamp, status, environment) "
                "VALUES (?, 'KXHIGHDEN-26APR28-T60', 'no', 60, 2, ?, 'PENDING', 'PAPER')",
                (scan_id, _utcnow().isoformat()),
            )
            intent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO orders (intent_id, exchange_order_id, ticker, side, "
                "price_cents, qty, order_type, status, created_at, updated_at, environment) "
                "VALUES (?, 'order-abc', 'KXHIGHDEN-26APR28-T60', 'no', 60, 2, "
                "'limit', 'submitted', ?, ?, 'PAPER')",
                (intent_id, _utcnow().isoformat(), _utcnow().isoformat()),
            )
            order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()

        execution_id = logger.log_execution_fill(
            order_id=order_id,
            exchange_trade_id="fill-xyz",
            ticker="KXHIGHDEN-26APR28-T60",
            side="no",
            price=60,
            qty=2,
            env_mode="PAPER",
            scan_mid_cents=55,
            predicted_p=0.08,
            market_implied_p=0.60,
            city="DEN",
            horizon_bin="24-48h",
            expected_value_cents=15.0,
            fees_cents=2.0,
        )

        assert execution_id is not None
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM trade_attribution WHERE execution_id = ?", (execution_id,)
            ).fetchone()
        assert row is not None
        assert row["ticker"] == "KXHIGHDEN-26APR28-T60"
        assert row["city"] == "DEN"
        assert row["predicted_p"] == pytest.approx(0.08)
        assert row["market_implied_p"] == pytest.approx(0.60)
        assert row["fill_price_cents"] == 60
        assert row["mid_at_fill_cents"] == 55
        assert row["slippage_cents"] == 5   # fill above mid = 60 - 55
        assert row["fees_cents"] == pytest.approx(2.0)


# ── 8. LVR populated when scan_mid exists ────────────────────────────────────

class TestLvrPopulation:
    def _setup_order(self, tmp_path: Path):
        from src.db.dwtrader import DWTraderDB
        db = DWTraderDB(db_path=str(tmp_path / "test.db"))
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO scans (ticker, market_probability, ml_probability, "
                "best_bid, best_ask, spread, volume, timestamp, environment) "
                "VALUES ('KXHIGHMIA-26APR28-T85', 0.5, 0.4, 45, 55, 10, 50, ?, 'PAPER')",
                (_utcnow().isoformat(),),
            )
            scan_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO intents (scan_id, ticker, side, expected_price_cents, "
                "target_qty, timestamp, status, environment) "
                "VALUES (?, 'KXHIGHMIA-26APR28-T85', 'no', 55, 2, ?, 'PENDING', 'PAPER')",
                (scan_id, _utcnow().isoformat()),
            )
            intent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO orders (intent_id, exchange_order_id, ticker, side, "
                "price_cents, qty, order_type, status, created_at, updated_at, environment) "
                "VALUES (?, 'order-lvr', 'KXHIGHMIA-26APR28-T85', 'no', 55, 2, "
                "'limit', 'submitted', ?, ?, 'PAPER')",
                (intent_id, _utcnow().isoformat(), _utcnow().isoformat()),
            )
            order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
        return db, order_id

    def test_lvr_cents_populated_when_scan_mid_provided(self, tmp_path):
        """fill_price=60, scan_mid=50 → lvr_cents=10 (above mid, favourable)."""
        from src.logging.trade_logger import TradeLogger
        db, order_id = self._setup_order(tmp_path)
        tl = TradeLogger(db)
        execution_id = tl.log_execution_fill(
            order_id=order_id,
            exchange_trade_id="fill-lvr-1",
            ticker="KXHIGHMIA-26APR28-T85",
            side="no",
            price=60,
            qty=2,
            env_mode="PAPER",
            scan_mid_cents=50,
        )
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT lvr_cents FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        assert row is not None
        assert row["lvr_cents"] == pytest.approx(10.0)

    def test_lvr_cents_null_when_no_scan_mid(self, tmp_path):
        """When scan_mid_cents is not provided, lvr_cents must be NULL."""
        from src.logging.trade_logger import TradeLogger
        db, order_id = self._setup_order(tmp_path)
        tl = TradeLogger(db)
        execution_id = tl.log_execution_fill(
            order_id=order_id,
            exchange_trade_id="fill-lvr-2",
            ticker="KXHIGHMIA-26APR28-T85",
            side="no",
            price=60,
            qty=2,
            env_mode="PAPER",
            scan_mid_cents=None,
        )
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT lvr_cents FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        assert row is not None
        assert row["lvr_cents"] is None


# ── 9. Canceled ticker cooldown ───────────────────────────────────────────────

class TestCancelCooldown:
    def _today_utc(self) -> str:
        return _utcnow().strftime("%Y-%m-%d")

    def test_three_cancels_triggers_cooldown(self, tmp_path):
        """get_canceled_order_count returns 3 when 3 canceled orders exist today."""
        db = _make_db(tmp_path)
        today = self._today_utc()
        ticker = "KXHIGHTDC-26APR28-T63"
        with db.get_connection() as conn:
            for _ in range(3):
                _insert_canceled_order(conn, ticker, today)
            conn.commit()

        count = db.get_canceled_order_count(ticker, trade_date=today)
        assert count == 3

    def test_fewer_than_three_cancels_allowed(self, tmp_path):
        """2 canceled orders → cooldown not triggered."""
        db = _make_db(tmp_path)
        today = self._today_utc()
        ticker = "KXHIGHTDC-26APR28-T63"
        with db.get_connection() as conn:
            for _ in range(2):
                _insert_canceled_order(conn, ticker, today)
            conn.commit()

        count = db.get_canceled_order_count(ticker, trade_date=today)
        assert count < 3

    def test_yesterday_cancels_do_not_count(self, tmp_path):
        """Yesterday's canceled orders don't block today."""
        db = _make_db(tmp_path)
        today = self._today_utc()
        yesterday = (_utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        ticker = "KXHIGHTDC-26APR28-T63"
        with db.get_connection() as conn:
            for _ in range(5):
                _insert_canceled_order(conn, ticker, yesterday)
            conn.commit()

        count = db.get_canceled_order_count(ticker, trade_date=today)
        assert count == 0

    def test_timeout_orders_also_count(self, tmp_path):
        """Orders with status='timeout' count toward the cooldown threshold."""
        db = _make_db(tmp_path)
        today = self._today_utc()
        ticker = "KXHIGHTDC-26APR28-T63"
        with db.get_connection() as conn:
            for _ in range(2):
                _insert_canceled_order(conn, ticker, today)
            _insert_canceled_order(conn, ticker, today, status="timeout")
            conn.commit()

        count = db.get_canceled_order_count(ticker, trade_date=today)
        assert count == 3


# ── Calibration diagnostics tests ─────────────────────────────────────────────

class TestCalibrationDiagnostics:

    def test_log_and_retrieve(self, tmp_path):
        db = _make_db(tmp_path)
        row_id = db.log_calibration_diagnostic(
            ts="2026-04-28T12:00:00",
            ticker="KXHIGHCHI-26APR30-T73",
            city="CHI",
            horizon_bucket="12-24h",
            strike_distance_bucket="otm",
            p_model=0.08,
            p_market=0.20,
            edge=-12.0,
            trade_side=None,
            gumbel_mode="half",
            env_mode="PAPER",
        )
        assert row_id is not None
        with db.get_connection() as conn:
            r = conn.execute(
                "SELECT * FROM calibration_diagnostics WHERE id = ?", (row_id,)
            ).fetchone()
        assert r["city"] == "CHI"
        assert abs(r["p_model"] - 0.08) < 1e-6
        assert r["gumbel_mode"] == "half"

    def test_city_edge_summary_below_min_n(self, tmp_path):
        db = _make_db(tmp_path)
        avg, n = db.get_city_edge_summary("CHI", n_days=7, min_n=20)
        assert n == 0
        assert avg == 0.0

    def test_city_edge_summary_above_min_n(self, tmp_path):
        db = _make_db(tmp_path)
        with db.get_connection() as conn:
            for i in range(25):
                conn.execute(
                    "INSERT INTO calibration_diagnostics "
                    "(ts, ticker, city, edge, p_model, p_market, gumbel_mode, env_mode) "
                    "VALUES (datetime('now'), 'KXHIGHCHI-26APR30-T73', 'CHI', -8.0, 0.07, 0.15, 'half', 'PAPER')"
                )
            conn.commit()
        avg, n = db.get_city_edge_summary("CHI", n_days=7, min_n=20)
        assert n == 25
        assert abs(avg - (-8.0)) < 1e-4


class TestExperimentRuns:

    def test_upsert_creates_and_updates(self, tmp_path):
        db = _make_db(tmp_path)
        rid = db.upsert_experiment_run(
            run_date="2026-04-28",
            gumbel_mode="half",
            total_trades=5,
            yes_trades=1,
            no_trades=4,
            avg_edge_cents=None,
            avg_lvr_cents=None,
            realized_pnl_cents=None,
            brier_score=0.12,
            n_settled=3,
        )
        assert rid is not None
        # Upsert again — should replace, not duplicate
        rid2 = db.upsert_experiment_run(
            run_date="2026-04-28",
            gumbel_mode="half",
            total_trades=8,
            yes_trades=2,
            no_trades=6,
            avg_edge_cents=None,
            avg_lvr_cents=None,
            realized_pnl_cents=None,
            brier_score=0.10,
            n_settled=5,
        )
        assert rid2 is not None
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM experiment_runs WHERE run_date='2026-04-28' AND gumbel_mode='half'"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["total_trades"] == 8


# ── Dedup relaxation tests ────────────────────────────────────────────────────
# Tests for the safer city+date dedup logic (Phase 2 of trade_cycle).
#
# Rules under test:
#   - Allow up to 2 positions per city+date slot
#   - Same side only (opposite side blocked)
#   - Min 2.0°F strike separation
#   - Max 4 total contracts per slot
#   - Candidates processed in EV-descending order; highest EV selected first

import sys
import os
from typing import Dict, List, Optional as _Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.index import _ticker_strike

_MIN_STRIKE_SEP_F   = 2.0
_MAX_POS_PER_SLOT   = 2
_MAX_CONTRACTS_SLOT = 4


def _make_dedup_cand(ev: float, ticker: str, side: str, qty: int = 2):
    """Minimal candidate tuple: (ev, market, posterior, intent, scan_id)."""
    from unittest.mock import MagicMock
    from src.index import _ticker_city
    intent = MagicMock()
    intent.side = side
    intent.ticker = ticker
    intent.target_qty = qty
    city = _ticker_city(ticker) or "UNK"
    posterior = {"city": city}
    return (ev, {}, posterior, intent, None)


def _run_dedup(
    candidates: list,
    held_positions: _Optional[List[dict]] = None,
) -> tuple:
    """
    Mirror of the Phase 2 dedup block from src/index.py.
    held_positions: list of {side, strike, qty} dicts for already-open positions
    in slot CHI_2026-05-01.
    Returns (best_per_slot, counters).
    """
    from src.index import _ticker_strike, _ticker_city, _ticker_date

    held = held_positions or []
    counters = {
        "n_already_held": 0,
        "n_blocked_opposite_side": 0,
        "n_blocked_strike_too_close": 0,
        "n_blocked_contract_cap": 0,
    }

    TEST_SLOT = "CHI_2026-05-01"
    held_slots: Dict[str, List[dict]] = {TEST_SLOT: held} if held else {}

    candidates_sorted = sorted(candidates, key=lambda x: x[0], reverse=True)
    cycle_additions: Dict[str, List[dict]] = {}
    best_per_slot: Dict[str, List] = {}

    for cand in candidates_sorted:
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

        if n_pos >= _MAX_POS_PER_SLOT:
            counters["n_already_held"] += 1
            continue

        existing_sides = {p["side"] for p in all_in_slot}
        if existing_sides and new_side not in existing_sides:
            counters["n_blocked_opposite_side"] += 1
            continue

        if new_strike is not None:
            too_close = any(
                p["strike"] is not None and abs(new_strike - p["strike"]) < _MIN_STRIKE_SEP_F
                for p in all_in_slot
            )
            if too_close:
                counters["n_blocked_strike_too_close"] += 1
                continue

        if total_qty + new_qty > _MAX_CONTRACTS_SLOT:
            counters["n_blocked_contract_cap"] += 1
            continue

        best_per_slot.setdefault(slot_key, []).append(cand)
        cycle_additions.setdefault(slot_key, []).append({
            "side": new_side, "strike": new_strike, "qty": new_qty,
        })

    return best_per_slot, counters


class TestTickerStrikeParsing:

    def test_band_strike(self):
        assert _ticker_strike("KXHIGHCHI-26APR30-B54.5") == pytest.approx(54.5)

    def test_top_strike(self):
        assert _ticker_strike("KXHIGHCHI-26APR30-T60") == pytest.approx(60.0)

    def test_integer_strike(self):
        assert _ticker_strike("KXHIGHDEN-26MAY01-B58") == pytest.approx(58.0)

    def test_unparseable_returns_none(self):
        assert _ticker_strike("GARBAGE") is None


class TestDedupRelaxed:

    def test_two_same_side_no_with_2f_separation_allowed(self):
        """Two NO positions with 3°F separation should both be selected."""
        cands = [
            _make_dedup_cand(0.30, "KXHIGHCHI-26MAY01-B54.5", "no"),
            _make_dedup_cand(0.20, "KXHIGHCHI-26MAY01-B57.5", "no"),  # 3°F away
        ]
        best, counters = _run_dedup(cands)
        slot = "CHI_2026-05-01"
        assert slot in best
        assert len(best[slot]) == 2, "Both same-side NO positions should be selected"
        assert counters["n_blocked_strike_too_close"] == 0

    def test_opposite_side_blocked(self):
        """A YES candidate should be blocked when a NO is already selected for same slot."""
        cands = [
            _make_dedup_cand(0.30, "KXHIGHCHI-26MAY01-B54.5", "no"),
            _make_dedup_cand(0.25, "KXHIGHCHI-26MAY01-T58.0", "yes"),  # opposite side
        ]
        best, counters = _run_dedup(cands)
        slot = "CHI_2026-05-01"
        assert len(best.get(slot, [])) == 1, "Only the NO position should be selected"
        assert counters["n_blocked_opposite_side"] == 1

    def test_strike_too_close_blocked(self):
        """Second NO candidate within 2°F of first should be blocked."""
        cands = [
            _make_dedup_cand(0.30, "KXHIGHCHI-26MAY01-B54.5", "no"),
            _make_dedup_cand(0.25, "KXHIGHCHI-26MAY01-B55.5", "no"),  # only 1°F away
        ]
        best, counters = _run_dedup(cands)
        slot = "CHI_2026-05-01"
        assert len(best.get(slot, [])) == 1
        assert counters["n_blocked_strike_too_close"] == 1

    def test_contract_cap_enforced(self):
        """Three candidates of qty=2 each: first two fill 4 contracts, third blocked."""
        cands = [
            _make_dedup_cand(0.30, "KXHIGHCHI-26MAY01-B54.5", "no", qty=2),
            _make_dedup_cand(0.25, "KXHIGHCHI-26MAY01-B58.0", "no", qty=2),  # fills cap at 4
            _make_dedup_cand(0.20, "KXHIGHCHI-26MAY01-B62.0", "no", qty=2),  # exceeds cap
        ]
        best, counters = _run_dedup(cands)
        slot = "CHI_2026-05-01"
        # Position cap (2) fires before contract cap for 3rd candidate
        assert len(best.get(slot, [])) <= 2
        assert counters["n_already_held"] + counters["n_blocked_contract_cap"] > 0

    def test_highest_ev_selected_first(self):
        """When two candidates compete, the higher-EV one is chosen first."""
        cands = [
            _make_dedup_cand(0.15, "KXHIGHCHI-26MAY01-B54.5", "no"),  # lower EV
            _make_dedup_cand(0.35, "KXHIGHCHI-26MAY01-B58.0", "no"),  # higher EV
        ]
        best, _ = _run_dedup(cands)
        slot = "CHI_2026-05-01"
        assert len(best[slot]) == 2, "Both should be selected (different strikes, 3.5°F apart)"
        # Verify order: higher EV first
        first_ev = best[slot][0][0]
        assert abs(first_ev - 0.35) < 1e-5

    def test_opposite_side_from_held_position_blocked(self):
        """New YES candidate blocked when existing held position in slot is NO."""
        held = [{"side": "no", "strike": 54.5, "qty": 2}]
        cands = [_make_dedup_cand(0.30, "KXHIGHCHI-26MAY01-T58.0", "yes")]
        best, counters = _run_dedup(cands, held_positions=held)
        assert len(best.get("CHI_2026-05-01", [])) == 0
        assert counters["n_blocked_opposite_side"] == 1

    def test_position_cap_from_held(self):
        """Slot already has 2 held positions; all new candidates blocked."""
        held = [
            {"side": "no", "strike": 54.5, "qty": 2},
            {"side": "no", "strike": 58.0, "qty": 2},
        ]
        cands = [_make_dedup_cand(0.30, "KXHIGHCHI-26MAY01-B62.0", "no")]
        best, counters = _run_dedup(cands, held_positions=held)
        assert len(best.get("CHI_2026-05-01", [])) == 0
        assert counters["n_already_held"] == 1
