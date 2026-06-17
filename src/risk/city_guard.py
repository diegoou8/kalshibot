"""
CityRiskGuard — adaptive city-level risk control based on rolling Brier score.

Blocks trading in a city for 24 hours when calibration degrades past thresholds.
Throttles position sizing when calibration is marginal.
State is persisted to data/city_blocks.json so blocks survive process restarts.

Thresholds (all tunable via constants):
  Brier < 0.20, n >= MIN_OBS  → full sizing (1.0×)
  0.20 <= Brier < 0.25        → throttled sizing (0.5×), log BRIER_THROTTLE_APPLIED
  Brier >= 0.25, n >= MIN_OBS → see env_mode below:
    LIVE  → 24h block,             log BLOCKED_CITY_BRIER_GUARD
    PAPER → 0.25× sizing throttle, log CITY_THROTTLED_PAPER_MODE  (no block)
  Tail risk (p<5% but YES, 2+ cases in last 20) → immediate 24h block regardless of mode
  n < MIN_OBS                 → monitor only, never block

PAPER-mode rationale: blocking deadlocks the calibration loop — cities can never
collect new settled trades to lower their Brier.  A heavy throttle (0.25×) keeps
the city trading at minimal size while letting calibration data accumulate.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_BLOCKS_FILE = Path(__file__).resolve().parents[2] / "data" / "city_blocks.json"

BRIER_THROTTLE: float = 0.20
BRIER_BLOCK: float = 0.25
MIN_OBS: int = 10
BLOCK_HOURS: int = 24
TAIL_RISK_THRESHOLD: int = 2
TAIL_RISK_P_THRESHOLD: float = 0.05
# Heavy throttle applied instead of a 24h block when running in PAPER/demo mode.
# Keeps the city active at minimal size so calibration data can accumulate.
PAPER_BRIER_THROTTLE: float = 0.25


class CityRiskGuard:
    """
    Adaptive per-city trading guard. Call refresh(db, cities) once per trade
    cycle to evaluate blocks, then check(city) per candidate.
    """

    def __init__(self, blocks_file: Optional[Path] = None):
        self._blocks_file: Path = blocks_file or _DEFAULT_BLOCKS_FILE
        self._blocks: Dict[str, str] = {}      # city → ISO UTC expiry datetime
        self._throttle: Dict[str, float] = {}  # city → size multiplier in {0.5, 1.0}
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if self._blocks_file.exists():
                self._blocks = json.loads(self._blocks_file.read_text())
        except Exception as e:
            logger.warning("CityRiskGuard: failed to load blocks file: %s", e)
            self._blocks = {}

    def _save(self) -> None:
        try:
            self._blocks_file.parent.mkdir(parents=True, exist_ok=True)
            self._blocks_file.write_text(json.dumps(self._blocks, indent=2))
        except Exception as e:
            logger.error("CityRiskGuard: failed to save blocks file: %s", e)

    # ── Block management ─────────────────────────────────────────────────────

    def _expire_blocks(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [
            city for city, until_str in self._blocks.items()
            if datetime.fromisoformat(until_str) <= now
        ]
        for city in expired:
            del self._blocks[city]
            logger.info("CITY_REACTIVATED: %s block expired — trading resumed", city)
        if expired:
            self._save()

    def block_city(self, city: str, reason: str) -> None:
        until = datetime.now(timezone.utc) + timedelta(hours=BLOCK_HOURS)
        self._blocks[city] = until.isoformat()
        self._save()
        logger.warning(
            "BLOCKED_CITY_%s: %s — blocked for %dh until %s UTC",
            reason, city, BLOCK_HOURS, until.strftime("%Y-%m-%d %H:%M"),
        )

    def is_blocked(self, city: str) -> bool:
        if city not in self._blocks:
            return False
        return datetime.now(timezone.utc) < datetime.fromisoformat(self._blocks[city])

    # ── Evaluation ───────────────────────────────────────────────────────────

    def refresh(self, db, cities: List[str], env_mode: str = "PAPER") -> None:
        """
        Evaluate each city and update block/throttle state.
        Must be called once per trade cycle before any check() calls.

        db:       DWTraderDB instance
        cities:   list of city codes to evaluate (e.g. list(_CITY_MAP.keys()))
        env_mode: execution mode string — "LIVE" enables 24h Brier blocks;
                  any other value (PAPER, DEMO, …) uses a 0.25× throttle instead.

        When the bot_config key BRIER_BLOCK_ENABLED is not set to "true" (the default
        during the Gumbel A/B/C experiment window through Jun 12 2026), all Brier-based
        throttling is skipped — only tail-risk blocks apply.  This preserves full-sized
        positions for all cities so experiment data is not cut short.  Flip the flag to
        "true" post-experiment to activate per-city Brier blocks.
        """
        # Read experiment-safe flag from DB (default False until explicitly enabled).
        brier_block_enabled = False
        try:
            raw = db.get_config("BRIER_BLOCK_ENABLED")
            brier_block_enabled = (raw or "").lower() == "true"
        except Exception as _e:
            logger.warning("CityRiskGuard: could not read BRIER_BLOCK_ENABLED: %s", _e)

        _is_live = env_mode.upper() == "LIVE"
        self._expire_blocks()
        self._throttle = {city: 1.0 for city in cities}

        for city in cities:
            if self.is_blocked(city):
                if not _is_live:
                    # Paper mode: release any existing block and fall through to the
                    # normal Brier evaluation below.  The block was likely set by a
                    # previous run that used the live-mode code path; in paper mode
                    # we throttle instead so calibration data can keep accumulating.
                    del self._blocks[city]
                    self._save()
                    logger.info(
                        "CITY_UNBLOCKED_PAPER_MODE: %s — prior block released, re-evaluating",
                        city,
                    )
                else:
                    self._throttle[city] = 0.0
                    continue

            brier, n = db.get_rolling_brier_by_city(city, window=30, min_obs=MIN_OBS)

            if brier is None or n < MIN_OBS:
                # Not enough data — monitor only, never block
                continue

            # Tail risk guard — checked before Brier block (more severe signal).
            # Applies regardless of brier_block_enabled: tail risk is a safety signal
            # that should fire even during the experiment window.
            tail_count = db.get_tail_risk_count(
                city, window=20, p_threshold=TAIL_RISK_P_THRESHOLD
            )
            if tail_count >= TAIL_RISK_THRESHOLD:
                logger.warning(
                    "BLOCKED_CITY_TAIL_RISK: %s — %d case(s) of p<%.0f%% but outcome=YES "
                    "in last 20 settled predictions",
                    city, tail_count, TAIL_RISK_P_THRESHOLD * 100,
                )
                if _is_live:
                    # Live mode: hard 24h block protects real capital
                    self.block_city(city, "TAIL_RISK")
                    self._throttle[city] = 0.0
                else:
                    # Paper mode: throttle to 0.25× instead of blocking so calibration
                    # data can accumulate (blocking deadlocks the loop — city never settles
                    # new trades to correct the signal).
                    self._throttle[city] = PAPER_BRIER_THROTTLE
                    logger.info(
                        "TAIL_RISK_THROTTLE_PAPER_MODE: %s — throttled to %.2f× (no block)",
                        city, PAPER_BRIER_THROTTLE,
                    )
                continue

            # Brier-based throttle/block — only active when BRIER_BLOCK_ENABLED=true.
            # During the experiment window this is skipped so all cities trade at full
            # size regardless of Brier, preserving data for post-experiment analysis.
            if not brier_block_enabled:
                if brier >= BRIER_BLOCK:
                    logger.info(
                        "BRIER_BLOCK_CANDIDATE: %s brier=%.3f (n=%d) — block deferred"
                        " (BRIER_BLOCK_ENABLED=false, experiment window)",
                        city, brier, n,
                    )
                elif brier >= BRIER_THROTTLE:
                    logger.debug(
                        "BRIER_THROTTLE_CANDIDATE: %s brier=%.3f (n=%d) — throttle deferred"
                        " (BRIER_BLOCK_ENABLED=false)",
                        city, brier, n,
                    )
                continue

            if brier >= BRIER_BLOCK:
                if _is_live:
                    # In live mode: hard 24h block protects real capital.
                    self.block_city(city, "BRIER_GUARD")
                    self._throttle[city] = 0.0
                else:
                    # In paper/demo mode: throttle to 0.25× instead of blocking.
                    # Blocking deadlocks calibration — city can never settle new trades
                    # to lower its Brier.  0.25× keeps it active at minimal size.
                    self._throttle[city] = PAPER_BRIER_THROTTLE
                    logger.info(
                        "CITY_THROTTLED_PAPER_MODE: %s brier=%.3f (n=%d) → %.2f× sizing"
                        " (no 24h block in paper mode)",
                        city, brier, n, PAPER_BRIER_THROTTLE,
                    )
                continue

            if brier >= BRIER_THROTTLE:
                self._throttle[city] = 0.5
                logger.info(
                    "BRIER_THROTTLE_APPLIED: %s brier=%.3f (n=%d) → 0.5× sizing",
                    city, brier, n,
                )

    def check(self, city: str) -> Tuple[bool, float]:
        """
        Returns (allow_trade, size_multiplier).

        allow_trade=False → skip this city entirely.
        size_multiplier: 1.0 (normal), 0.5 (throttled), 0.0 (blocked).

        refresh() must have been called this cycle for the result to be current.
        For unknown cities (not in cities passed to refresh), defaults to (True, 1.0).
        """
        if self.is_blocked(city):
            return False, 0.0
        mult = self._throttle.get(city, 1.0)
        if mult <= 0.0:
            return False, 0.0
        return True, mult
