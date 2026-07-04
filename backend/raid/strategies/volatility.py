"""RAID-C5 — Volatility Compression → Expansion. Detects a measurable volatility
squeeze (low Bollinger bandwidth / low Donchian width) and positions for the
expansion breakout in the higher-timeframe direction. Distinct from C1: C1 needs an
established uptrend + resistance break; C5 fires specifically on a regime *transition*
out of compression, in either direction (long sleeve paper, short sleeve shadow)."""

from __future__ import annotations

from typing import Optional

from raid.core.candidate import Candidate, Direction, EntryType, MarketRegime
from raid.core.provider import CAP_SPOT_LONG
from raid.core.strategy import ExitDecision, Strategy, StrategyContext
from raid.strategies.helpers import build_candidate, atr_scaled_stop_dist, rr_honest_target_dist

CODE_VERSION = "omega-0.1.0"
_TF = "5m"
_MIN_NET_RR = 1.30
# Compression thresholds: bandwidth below this fraction = squeezed.
_BB_SQUEEZE = 0.02
_DONCHIAN_SQUEEZE = 0.02


class C5VolatilityExpansion(Strategy):
    strategy_id = "RAID-C5"
    version = CODE_VERSION
    required_capabilities = frozenset({CAP_SPOT_LONG})
    # Fires as the market leaves compression; RANGE (pre-break) is the setup regime.
    eligible_regimes = frozenset({MarketRegime.RANGE})

    def generate_candidates(self, ctx: StrategyContext) -> list[Candidate]:
        f = ctx.feature(_TF)
        if f is None or f.bb_bandwidth is None or f.donchian_pct is None:
            return []
        if f.swing_high is None or f.ema20 is None or f.ema50 is None:
            return []
        # Require a genuine squeeze on BOTH measures (the compression regime).
        if f.bb_bandwidth > _BB_SQUEEZE or f.donchian_pct > _DONCHIAN_SQUEEZE:
            return []
        # Long-only sleeve: take the upside expansion when the short-term bias is up.
        if not (f.ema20 >= f.ema50):
            return []
        px = f.last_price
        upper = f.swing_high
        if px > upper * 1.002:                   # already expanded — missed it
            return []
        trigger = upper * 1.001                  # breakout of the squeeze box
        stop_dist = atr_scaled_stop_dist(ctx, f.atr_pct)        # 1.5x 1h-ATR, bounded [0.6%,4%]
        stop = trigger * (1 - stop_dist)
        target = trigger * (1 + rr_honest_target_dist(stop_dist))   # TP scaled to net_rr 1.35 (honest)
        c = build_candidate(
            strategy_id=self.strategy_id, strategy_version=self.version, code_version=CODE_VERSION,
            ctx=ctx, direction=Direction.LONG, entry_type=EntryType.STOP, timeframe=_TF,
            reference_price=px, stop_price=stop, targets=(target,), trigger_price=trigger,
            expiry_ts=ctx.extras.get("expiry_ts", ctx.timestamp),
            capability_requirements=(CAP_SPOT_LONG,), min_net_rr=_MIN_NET_RR,
        )
        return [c] if c else []

    def should_exit(self, position, ctx: StrategyContext) -> Optional[ExitDecision]:
        if ctx.market_regime == MarketRegime.CRISIS:
            return ExitDecision(True, "crisis_regime", "immediate")
        return None
