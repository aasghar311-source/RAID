"""Trend strategies: RAID-C1 (long breakout), C2 (long pullback), C3 (short breakdown).

All logic is deterministic functions of the feature snapshots. Entries, stops, and
targets come from structure (swing levels, EMAs, ATR) — never from an LLM. C3 requires
the `short` capability, so it stays shadow-only until shorting is operator-enabled.
"""

from __future__ import annotations

import logging
from typing import Optional

import config
from raid.core.candidate import Candidate, Direction, EntryType, MarketRegime
from raid.core.provider import CAP_SHORT, CAP_SPOT_LONG
from raid.core.strategy import ExitDecision, Strategy, StrategyContext
from raid.strategies.helpers import build_candidate, atr_scaled_stop_dist, rr_honest_target_dist

log = logging.getLogger("raid.strategies.trend")

CODE_VERSION = "omega-0.1.0"
_PRIMARY_TF = "5m"
# C1 breakout volume confirmation: the latest 5m bar must trade at >= this multiple of the
# trailing 20-bar average volume (real breakouts expand volume). Cuts false breakouts.
_VOLUME_CONFIRM_MULT = 1.5


def _volume_confirmed(candles, mult: float = _VOLUME_CONFIRM_MULT) -> tuple[bool, float]:
    """(confirmed, ratio) from the raw 5m candles ([...,volume] at index 5). Returns
    (True, ratio) when the latest bar's volume >= mult x the prior-20 average; (False, 0.0)
    when volume is missing / insufficient / zero-average — FAIL CLOSED (aligned with the shared
    hard-zero gate in helpers.build_candidate). The 1.5x threshold for positive-volume bars is
    unchanged."""
    rows = candles or []
    if len(rows) < 21:
        return False, 0.0
    try:
        vols = [float(r[5]) for r in rows[-21:] if len(r) > 5]
    except (TypeError, ValueError):
        return False, 0.0
    if len(vols) != 21:
        return False, 0.0
    avg = sum(vols[:-1]) / 20.0
    if avg <= 0:
        return False, 0.0
    ratio = vols[-1] / avg
    return ratio >= mult, ratio

# C1/C3 now size the stop off the 1h ATR via helpers.atr_scaled_stop_dist (1.5x, bounded
# [0.6%,4%]) and set the TP via rr_honest_target_dist. _RR_TARGET is retained only for C2, whose
# stop is STRUCTURAL (swing-low/ema20), not the flat ATR floor.
_RR_TARGET = 4.0           # C2 structural-stop target multiple (gross reward = 4x gross risk)
_MIN_NET_RR = 1.25


class C1LongTrendBreakout(Strategy):
    strategy_id = "RAID-C1"
    version = CODE_VERSION
    required_capabilities = frozenset({CAP_SPOT_LONG})
    eligible_regimes = frozenset()   # Stage-D: gated by the reconciled SPINE, not the legacy regime
    atr_scaled_stop = True   # stop = 1.5x 1h-ATR -> graduated cost/R gate applies

    def generate_candidates(self, ctx: StrategyContext) -> list[Candidate]:
        # Stage-D: direction from the reconciled per-pair spine — fire ONLY when the pair resolves
        # LONG (portfolio RISK_ON/MIXED + the pair's own up-structure). Never a long on a RISK_OFF book.
        if ctx.extras.get("spine_dir") != "LONG":
            return []
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
        # §10 volume override — real breakouts expand volume, measured on the COMPLETED bar (the runner
        # threads vol_ratio_completed so we never read the partial forming bar). 1.50x is reachable for
        # longs (harness vr@LONG p90~2.1) — deliberately NOT C3's 0.70x (short breakdowns grind).
        vrc = ctx.extras.get("vol_ratio_completed")
        if vrc is None or vrc < config.C1_VOLUME_MULT:
            log.info("C1: skip %s — breakout volume %s < %.2fx (completed-bar)", ctx.symbol,
                     ("%.2f" % vrc if vrc is not None else "NA"), config.C1_VOLUME_MULT)
            return []
        trigger = resistance * 1.001            # confirm the breakout
        stop_dist = atr_scaled_stop_dist(ctx, f.atr_pct)            # 1.5x 1h-ATR, bounded [0.6%,4%]
        stop = trigger * (1 - stop_dist)
        target = trigger * (1 + rr_honest_target_dist(stop_dist))   # TP scaled to net_rr 1.35 (honest)
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
    eligible_regimes = frozenset()   # Stage-D: gated by the reconciled SPINE, not the legacy regime

    def generate_candidates(self, ctx: StrategyContext) -> list[Candidate]:
        # Stage-D: direction from the reconciled per-pair spine — buy pullbacks ONLY when the pair
        # resolves LONG (portfolio RISK_ON/MIXED + up-structure). Never a long on a RISK_OFF book.
        if ctx.extras.get("spine_dir") != "LONG":
            return []
        f = ctx.feature(_PRIMARY_TF)
        if f is None or f.ema20 is None or f.ema50 is None or f.swing_low is None:
            return []
        if not (f.ema20 > f.ema50):
            return []
        # §10 pullback MINIMUM (completed-bar) — a pullback is a dip, so this only rejects DEAD-volume
        # pullbacks, it does NOT require expansion (that would filter healthy dips). Low floor by design.
        vrc = ctx.extras.get("vol_ratio_completed")
        if vrc is None or vrc < config.C2_VOLUME_MULT:
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
    required_capabilities = frozenset({CAP_SHORT})
    eligible_regimes = frozenset()   # Stage-D: gated by the reconciled SPINE, not the legacy regime
    atr_scaled_stop = True   # stop = 1.5x 1h-ATR -> graduated cost/R gate applies

    def generate_candidates(self, ctx: StrategyContext) -> list[Candidate]:
        # Stage-D: direction from the reconciled per-pair spine — fire ONLY when the pair resolves
        # SHORT (portfolio RISK_OFF/MIXED + the pair's own down-structure). Never a short on a RISK_ON book.
        if ctx.extras.get("spine_dir") != "SHORT":
            return []
        f = ctx.feature(_PRIMARY_TF)
        if f is None or f.swing_low is None or f.ema20 is None or f.ema50 is None:
            return []
        if not (f.ema20 < f.ema50):              # stacked down
            return []
        support = f.swing_low
        px = f.last_price
        if not (support * 0.998 <= px <= support * 1.015):
            return []
        # §10 volume override — the breakdown must expand volume, measured on the COMPLETED bar (the
        # runner threads vol_ratio_completed so we never read the partial forming bar).
        vrc = ctx.extras.get("vol_ratio_completed")
        if vrc is None or vrc < config.C3_VOLUME_MULT:
            log.info("C3: skip %s — breakdown volume %s < %.2fx (completed-bar)", ctx.symbol,
                     ("%.2f" % vrc if vrc is not None else "NA"), config.C3_VOLUME_MULT)
            return []
        trigger = support * 0.999                # confirm the breakdown
        stop_dist = atr_scaled_stop_dist(ctx, f.atr_pct)            # 1.5x 1h-ATR, bounded [0.6%,4%]
        stop = trigger * (1 + stop_dist)
        target = trigger * (1 - rr_honest_target_dist(stop_dist))   # TP scaled to net_rr 1.35 (honest)
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
