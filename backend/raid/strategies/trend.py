"""Trend strategies: RAID-C1 (long breakout), C2 (long pullback), C3 (short breakdown).

All logic is deterministic functions of the feature snapshots. Entries, stops, and
targets come from structure (swing levels, EMAs, ATR) — never from an LLM. C3 requires
the `short` capability, so it stays shadow-only until shorting is operator-enabled.
"""

from __future__ import annotations

from typing import Optional

from raid.core.candidate import Candidate, Direction, EntryType, MarketRegime
from raid.core.provider import CAP_SHORT, CAP_SPOT_LONG
from raid.core.strategy import ExitDecision, Strategy, StrategyContext
from raid.strategies.helpers import build_candidate

CODE_VERSION = "omega-0.1.0"
_PRIMARY_TF = "5m"

# Stop distance bounds (fraction of price) derived from ATR, clamped for sanity.
_STOP_MIN = 0.006
_STOP_MAX = 0.020
_RR_TARGET = 2.5            # gross reward = 2.5x gross risk (nets ~1.5 R:R after 0.32% round-trip)
_MIN_NET_RR = 1.25


def _atr_stop_dist(atr_pct: Optional[float]) -> float:
    base = atr_pct if atr_pct is not None else _STOP_MIN
    return min(max(base, _STOP_MIN), _STOP_MAX)


class C1LongTrendBreakout(Strategy):
    strategy_id = "RAID-C1"
    version = CODE_VERSION
    required_capabilities = frozenset({CAP_SPOT_LONG})
    eligible_regimes = frozenset({MarketRegime.TREND_UP})

    def generate_candidates(self, ctx: StrategyContext) -> list[Candidate]:
        f = ctx.feature(_PRIMARY_TF)
        if f is None or f.swing_high is None or f.ema20 is None or f.ema50 is None:
            return []
        if not (f.ema20 > f.ema50):            # trend must be stacked up
            return []
        resistance = f.swing_high
        px = f.last_price
        # Actionable only when price is just below/at resistance (breakout imminent),
        # not already extended far above it.
        if not (resistance * 0.985 <= px <= resistance * 1.002):
            return []
        trigger = resistance * 1.001            # confirm the breakout
        stop_dist = _atr_stop_dist(f.atr_pct)
        stop = trigger * (1 - stop_dist)
        risk = trigger - stop
        target = trigger + _RR_TARGET * risk
        c = build_candidate(
            strategy_id=self.strategy_id, strategy_version=self.version, code_version=CODE_VERSION,
            ctx=ctx, direction=Direction.LONG, entry_type=EntryType.STOP, timeframe=_PRIMARY_TF,
            reference_price=px, stop_price=stop, targets=(target,), trigger_price=trigger,
            expiry_ts=ctx.extras.get("expiry_ts", ctx.timestamp),
            capability_requirements=(CAP_SPOT_LONG,), min_net_rr=_MIN_NET_RR,
        )
        return [c] if c else []

    def should_exit(self, position, ctx: StrategyContext) -> Optional[ExitDecision]:
        if ctx.market_regime in (MarketRegime.TREND_DOWN, MarketRegime.CRISIS):
            return ExitDecision(True, "regime_flipped_adverse", "immediate")
        return None


class C2LongTrendPullback(Strategy):
    strategy_id = "RAID-C2"
    version = CODE_VERSION
    required_capabilities = frozenset({CAP_SPOT_LONG})
    eligible_regimes = frozenset({MarketRegime.TREND_UP})

    def generate_candidates(self, ctx: StrategyContext) -> list[Candidate]:
        f = ctx.feature(_PRIMARY_TF)
        if f is None or f.ema20 is None or f.ema50 is None or f.swing_low is None:
            return []
        if not (f.ema20 > f.ema50):
            return []
        px = f.last_price
        ema20 = f.ema20
        # Price should be ABOVE ema20 and pulling back toward it (within a small band).
        if not (ema20 < px <= ema20 * 1.02):
            return []
        limit = ema20                            # buy the pullback to support
        stop = min(f.swing_low, ema20) * (1 - 0.003)
        if stop >= limit:
            return []
        risk = limit - stop
        target = limit + _RR_TARGET * risk
        c = build_candidate(
            strategy_id=self.strategy_id, strategy_version=self.version, code_version=CODE_VERSION,
            ctx=ctx, direction=Direction.LONG, entry_type=EntryType.LIMIT, timeframe=_PRIMARY_TF,
            reference_price=px, stop_price=stop, targets=(target,), limit_price=limit,
            expiry_ts=ctx.extras.get("expiry_ts", ctx.timestamp),
            capability_requirements=(CAP_SPOT_LONG,), min_net_rr=_MIN_NET_RR,
        )
        return [c] if c else []

    def should_exit(self, position, ctx: StrategyContext) -> Optional[ExitDecision]:
        if ctx.market_regime in (MarketRegime.TREND_DOWN, MarketRegime.CRISIS):
            return ExitDecision(True, "regime_flipped_adverse", "immediate")
        return None


class C3ShortTrendBreakdown(Strategy):
    strategy_id = "RAID-C3"
    version = CODE_VERSION
    required_capabilities = frozenset({CAP_SHORT})   # shadow-only until short enabled
    eligible_regimes = frozenset({MarketRegime.TREND_DOWN})

    def generate_candidates(self, ctx: StrategyContext) -> list[Candidate]:
        f = ctx.feature(_PRIMARY_TF)
        if f is None or f.swing_low is None or f.ema20 is None or f.ema50 is None:
            return []
        if not (f.ema20 < f.ema50):              # stacked down
            return []
        support = f.swing_low
        px = f.last_price
        if not (support * 0.998 <= px <= support * 1.015):
            return []
        trigger = support * 0.999                # confirm the breakdown
        stop_dist = _atr_stop_dist(f.atr_pct)
        stop = trigger * (1 + stop_dist)
        risk = stop - trigger
        target = trigger - _RR_TARGET * risk
        if target <= 0:
            return []
        c = build_candidate(
            strategy_id=self.strategy_id, strategy_version=self.version, code_version=CODE_VERSION,
            ctx=ctx, direction=Direction.SHORT, entry_type=EntryType.STOP, timeframe=_PRIMARY_TF,
            reference_price=px, stop_price=stop, targets=(target,), trigger_price=trigger,
            expiry_ts=ctx.extras.get("expiry_ts", ctx.timestamp),
            capability_requirements=(CAP_SHORT,), min_net_rr=_MIN_NET_RR,
        )
        return [c] if c else []

    def should_exit(self, position, ctx: StrategyContext) -> Optional[ExitDecision]:
        if ctx.market_regime in (MarketRegime.TREND_UP, MarketRegime.CRISIS):
            return ExitDecision(True, "regime_flipped_adverse", "immediate")
        return None
