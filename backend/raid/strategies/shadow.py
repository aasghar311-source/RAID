"""Shadow-tier strategies (Section 8: C6–C10).

These conform fully to the Strategy interface and register, but their candidate
generation requires inputs the single-symbol context / current data feeds do not yet
provide. Rather than fabricate signals, each declines (returns []) and records the
precise missing dependency. They advance to real logic when Phase-3 data (universe
snapshots, cointegration series, funding/basis, order-book microstructure) lands and
the relevant capability is enabled. They never trade paper capital in this state.

This is honest scaffolding — the classes exist and are wired, but make NO claim to
generate real edge until their data contract is satisfied.
"""

from __future__ import annotations

from typing import Optional

from raid.core.candidate import Candidate, MarketRegime
from raid.core.provider import CAP_FUTURES, CAP_MARGIN, CAP_SHORT, CAP_SPOT_LONG
from raid.core.strategy import ExitDecision, Strategy, StrategyContext

CODE_VERSION = "omega-0.1.0"


class _ShadowStrategy(Strategy):
    """Base for strategies awaiting a data/capability contract. Declines cleanly."""

    requires: str = "unspecified data contract"

    def generate_candidates(self, ctx: StrategyContext) -> list[Candidate]:
        # No-trade until the documented dependency is satisfied. Recorded in extras
        # so a data-quality/coverage report can surface why nothing was produced.
        ctx.extras.setdefault("_shadow_declined", {})[self.strategy_id] = self.requires
        return []

    def should_exit(self, position, ctx: StrategyContext) -> Optional[ExitDecision]:
        return None


class C6RelativeStrengthRotation(_ShadowStrategy):
    strategy_id = "RAID-C6"
    version = CODE_VERSION
    required_capabilities = frozenset({CAP_SPOT_LONG})
    eligible_regimes = frozenset({MarketRegime.TREND_UP})
    requires = "universe_snapshot with per-symbol risk-adjusted momentum + breadth (Phase 3)"


class C7CrossSectionalMomentum(_ShadowStrategy):
    strategy_id = "RAID-C7"
    version = CODE_VERSION
    required_capabilities = frozenset({CAP_SPOT_LONG})
    eligible_regimes = frozenset({MarketRegime.TREND_UP, MarketRegime.RANGE})
    requires = "cross-sectional ranking over the full eligible universe + portfolio construction (Phase 3)"


class C8StatisticalPairs(_ShadowStrategy):
    strategy_id = "RAID-C8"
    version = CODE_VERSION
    required_capabilities = frozenset({CAP_SPOT_LONG, CAP_SHORT})
    eligible_regimes = frozenset({MarketRegime.RANGE, MarketRegime.VOLATILE})
    requires = "rolling cointegration + spread half-life for a validated pair; two-leg execution (Phase 3 + short capability)"


class C9FundingBasisCarry(_ShadowStrategy):
    strategy_id = "RAID-C9"
    version = CODE_VERSION
    required_capabilities = frozenset({CAP_SPOT_LONG, CAP_FUTURES, CAP_MARGIN})
    eligible_regimes = frozenset({MarketRegime.RANGE, MarketRegime.TREND_UP, MarketRegime.TREND_DOWN})
    requires = "real funding/basis series + spot & hedge legs + borrow/margin model (Phase 3 + futures capability)"


class C10LiquiditySweepReversal(_ShadowStrategy):
    strategy_id = "RAID-C10"
    version = CODE_VERSION
    required_capabilities = frozenset({CAP_SPOT_LONG})
    eligible_regimes = frozenset({MarketRegime.VOLATILE, MarketRegime.CRISIS})
    requires = "order-book depletion + rejection-wick microstructure with real depth history (Phase 3 live collection)"
