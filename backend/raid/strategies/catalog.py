"""Default strategy registry — registers all ten RAID-C strategies.

Every strategy starts in SHADOW. Promotion to PAPER (portfolio capital) is
evidence-gated by the Phase-6 promotion engine — nothing auto-trades paper capital
just because it was registered. This is the conservative, fail-closed default.
"""

from __future__ import annotations

from raid.core.registry import StrategyRegistry
from raid.core.strategy import StrategyMode
from raid.strategies.meanrev import C4RangeMeanReversion
from raid.strategies.shadow import (
    C6RelativeStrengthRotation, C7CrossSectionalMomentum, C8StatisticalPairs,
    C9FundingBasisCarry, C10LiquiditySweepReversal,
)
from raid.strategies.trend import (
    C1LongTrendBreakout, C2LongTrendPullback, C3ShortTrendBreakdown,
)
from raid.strategies.volatility import C5VolatilityExpansion

ALL_STRATEGY_IDS = [f"RAID-C{i}" for i in range(1, 11)]

# Strategies with real single-symbol candidate logic today (spot-long, paper-eligible
# once promoted). The rest are shadow pending data/capability contracts.
FUNCTIONAL_LONG = {"RAID-C1", "RAID-C2", "RAID-C4", "RAID-C5"}


def build_default_registry() -> StrategyRegistry:
    reg = StrategyRegistry()
    reg.register(C1LongTrendBreakout(), StrategyMode.SHADOW)
    reg.register(C2LongTrendPullback(), StrategyMode.SHADOW)
    reg.register(C3ShortTrendBreakdown(), StrategyMode.SHADOW)
    reg.register(C4RangeMeanReversion(), StrategyMode.SHADOW)
    reg.register(C5VolatilityExpansion(), StrategyMode.SHADOW)
    reg.register(C6RelativeStrengthRotation(), StrategyMode.SHADOW)
    reg.register(C7CrossSectionalMomentum(), StrategyMode.SHADOW)
    reg.register(C8StatisticalPairs(), StrategyMode.SHADOW)
    reg.register(C9FundingBasisCarry(), StrategyMode.SHADOW)
    reg.register(C10LiquiditySweepReversal(), StrategyMode.SHADOW)
    return reg
