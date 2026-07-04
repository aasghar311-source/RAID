"""Strategy interface (Section 8) — the contract every RAID-C strategy implements.

Strategies are deterministic candidate generators. They never size positions (the
risk manager does), never place orders, and never call an LLM in the decision path.
A strategy either emits fully-typed Candidates or explicitly declines (no-trade).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional

from raid.core.candidate import Candidate, MarketRegime, Rejection
from raid.core.features import FeatureSnapshot


class StrategyMode(str, Enum):
    DISABLED = "disabled"        # not running
    SHADOW = "shadow"            # generates + scores candidates, no portfolio capital
    PAPER = "paper"              # paper capital via the risk manager
    QUARANTINED = "quarantined"  # forced to shadow after degradation; history kept


@dataclass(frozen=True)
class StrategyContext:
    """Everything a strategy needs to decide, at one instant. Immutable snapshot."""

    symbol: str
    instrument_id: str
    timestamp: str
    market_regime: MarketRegime
    features: dict[str, FeatureSnapshot]      # keyed by timeframe, e.g. "5m","1h","4h"
    market_data_snapshot_id: str
    reference_price: Decimal
    spread_pct: float
    depth_ok: bool
    btc_regime: MarketRegime = MarketRegime.UNKNOWN
    capabilities: frozenset[str] = frozenset()  # e.g. {"spot_long","short","margin"}
    extras: dict = field(default_factory=dict)

    def feature(self, timeframe: str) -> Optional[FeatureSnapshot]:
        return self.features.get(timeframe)


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str
    urgency: str = "normal"   # normal | immediate


@dataclass(frozen=True)
class Action:
    """A protective/management action a strategy requests for an open position."""

    kind: str                 # e.g. "move_stop", "take_partial", "tighten_time_stop"
    detail: dict = field(default_factory=dict)


class Strategy(ABC):
    """Base class for all RAID-C strategies. Subclasses set strategy_id/version and
    implement the eligibility + candidate-generation + exit logic."""

    strategy_id: str = "abstract"
    version: str = "0.0.0"
    required_capabilities: frozenset[str] = frozenset({"spot_long"})
    eligible_regimes: frozenset[MarketRegime] = frozenset()
    # True for strategies whose stop = atr_scaled_stop_dist (1.5x 1h-ATR) and whose TP pins
    # net_rr at 1.35 -> the net_rr gate is blind to absolute cost load, so the graduated
    # cost/R gate (runner) applies. Structural-stop strategies leave this False (exempt).
    atr_scaled_stop: bool = False

    # --- eligibility ----------------------------------------------------------
    def is_eligible(self, ctx: StrategyContext) -> bool:
        """Default: regime must be in eligible_regimes, capabilities satisfied,
        spread/depth acceptable. Subclasses may tighten but should call super()."""
        if self.eligible_regimes and ctx.market_regime not in self.eligible_regimes:
            return False
        if not self.required_capabilities.issubset(ctx.capabilities):
            return False
        if not ctx.depth_ok:
            return False
        return True

    # --- candidate generation -------------------------------------------------
    @abstractmethod
    def generate_candidates(self, ctx: StrategyContext) -> list[Candidate]:
        """Return zero or more fully-typed candidates. Empty list is a valid
        no-trade outcome. Must never return a partially-built or repaired candidate."""

    def validate_candidate(self, candidate: Candidate, ctx: StrategyContext):
        """Second-pass validation hook. Default: the Candidate model already
        validated structurally at construction, so accept. Override to add
        strategy-specific invalidation (returns the Candidate or a Rejection)."""
        return candidate

    # --- position management --------------------------------------------------
    def manage_position(self, position, ctx: StrategyContext) -> list[Action]:
        """Return protective actions for an open position (default: none)."""
        return []

    @abstractmethod
    def should_exit(self, position, ctx: StrategyContext) -> Optional[ExitDecision]:
        """Return an ExitDecision to close, or None to hold. Each strategy owns its
        exits (no universal exit system)."""

    # --- explainability -------------------------------------------------------
    def explain_decision(self, candidate: Candidate, ctx: StrategyContext) -> str:
        return f"{self.strategy_id} v{self.version} {candidate.direction.value} {candidate.symbol}"
