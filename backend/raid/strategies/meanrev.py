"""RAID-C4 — Range Mean Reversion. Buys near a validated range low in a RANGE regime
with a leaning-oversold RSI, targeting the range mid/high. Strict invalidation just
below support; no averaging down. Long sleeve is paper; shorts are shadow-only."""

from __future__ import annotations

from typing import Optional

from raid.core.candidate import Candidate, Direction, EntryType, MarketRegime
from raid.core.provider import CAP_SPOT_LONG
from raid.core.strategy import ExitDecision, Strategy, StrategyContext
from raid.strategies.helpers import build_candidate

CODE_VERSION = "omega-0.1.0"
_TF = "5m"
_MIN_NET_RR = 1.20


class C4RangeMeanReversion(Strategy):
    strategy_id = "RAID-C4"
    version = CODE_VERSION
    required_capabilities = frozenset({CAP_SPOT_LONG})
    eligible_regimes = frozenset({MarketRegime.RANGE})

    def generate_candidates(self, ctx: StrategyContext) -> list[Candidate]:
        f = ctx.feature(_TF)
        if f is None or f.swing_low is None or f.swing_high is None or f.rsi14 is None:
            return []
        lo, hi, px = f.swing_low, f.swing_high, f.last_price
        if hi <= lo:
            return []
        # Must be a real range and price must be in the lower third, leaning oversold.
        band = (hi - lo) / lo
        if band < 0.01 or band > 0.10:          # too tight to trade / too wide = not a range
            return []
        if px > lo + 0.33 * (hi - lo):          # only near the low
            return []
        if f.rsi14 > 45:                        # leaning oversold
            return []
        limit = lo * 1.002                       # bid just above support
        stop = lo * (1 - 0.006)                  # invalidation below support
        if stop >= limit:
            return []
        mid = (hi + lo) / 2
        target = min(mid, limit * 1.03)          # reversion to mid, capped
        c = build_candidate(
            strategy_id=self.strategy_id, strategy_version=self.version, code_version=CODE_VERSION,
            ctx=ctx, direction=Direction.LONG, entry_type=EntryType.LIMIT, timeframe=_TF,
            reference_price=px, stop_price=stop, targets=(target,), limit_price=limit,
            expiry_ts=ctx.extras.get("expiry_ts", ctx.timestamp),
            capability_requirements=(CAP_SPOT_LONG,), min_net_rr=_MIN_NET_RR,
        )
        return [c] if c else []

    def should_exit(self, position, ctx: StrategyContext) -> Optional[ExitDecision]:
        # Range broke into a trend/crisis -> the mean-reversion thesis is void.
        if ctx.market_regime in (MarketRegime.TREND_DOWN, MarketRegime.TREND_UP,
                                 MarketRegime.CRISIS, MarketRegime.VOLATILE):
            return ExitDecision(True, "range_broken", "immediate")
        return None
