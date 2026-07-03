"""Shadow-tier strategies still awaiting a data/capability contract: C8, C9.

These conform fully to the Strategy interface and register, but their candidate
generation requires inputs the current data feeds / capabilities do not yet provide
(cointegration spreads for C8; real funding/basis series + margin for C9). Rather than
fabricate signals, each declines (returns []) and records the precise missing
dependency. They never trade paper capital in this state.

C6, C7 and C10 previously lived here; they have been activated to paper (real logic in
raid/strategies/rotation.py and raid/strategies/sweep.py). C8 (needs short capability)
and C9 (needs futures/margin) remain honest scaffolding until their contract is met.
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
