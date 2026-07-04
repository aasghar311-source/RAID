"""Cross-sectional long strategies: RAID-C6 (relative-strength rotation) and
RAID-C7 (cross-sectional momentum).

Both consume the per-cycle universe ranking the runner computes once and threads in
via ctx.extras['universe_rankings'] (see raid.core.universe). They are distinct:

  * C6 ROTATES INTO the very strongest names (top-5 by risk-adjusted momentum) that
    are themselves in a TREND_UP regime, on a throttled ~2h rebalance cadence so it
    never churns fees every 20-minute cycle. The runner also rotates C6 positions OUT
    when they fall out of the leaderboard.
  * C7 formally ranks the whole universe and holds the top quintile (momentum
    persistence) in TREND_UP or RANGE; it does not re-add a name it already holds, and
    bottom-quintile names are recorded as SHORT candidates in SHADOW only (no shorts
    until margin is operator-enabled).

Neither sizes positions (the risk manager does) and neither ever opens a symbol that
already has an open position (no stacking; also dedupes C6 vs C7 across cycles).
"""

from __future__ import annotations

from typing import Optional

from raid.core.candidate import Candidate, Direction, EntryType, MarketRegime
from raid.core.provider import CAP_SHORT, CAP_SPOT_LONG
from raid.core.strategy import ExitDecision, Strategy, StrategyContext
from raid.strategies.helpers import build_candidate, atr_scaled_stop_dist, rr_honest_target_dist

CODE_VERSION = "omega-0.2.0"
_SETUP_TF = "1h"          # ranking horizon = hourly
_MIN_NET_RR = 1.20
_MIN_TREND_QUALITY = 0.15  # R^2 floor — rank on a real move, not noise
# C6/C7 size the stop off the 1h ATR via helpers.atr_scaled_stop_dist (1.5x, bounded [0.6%,4%])
# and set the TP via rr_honest_target_dist — no local flat-floor constants anymore.

C6_TOP_N = 5              # C6 only rotates into the top-5
C6_REBALANCE_HOURS = 2.0  # throttle: no new C6 entry within this window


def _rankings(ctx: StrategyContext) -> dict:
    return ctx.extras.get("universe_rankings") or {}


def _already_open(ctx: StrategyContext) -> bool:
    return ctx.symbol in (ctx.extras.get("open_symbols") or set())


def _long_market_candidate(strategy_id: str, ctx: StrategyContext) -> Optional[Candidate]:
    """Build a risk-sized long MARKET candidate at the live price with an ATR stop and
    an R-multiple target. Returns None if features are missing or it's uneconomic."""
    f5 = ctx.feature("5m")
    if f5 is None:
        return None
    px = float(ctx.reference_price)
    if px <= 0:
        return None
    stop_dist = atr_scaled_stop_dist(ctx, f5.atr_pct)     # 1.5x 1h-ATR, bounded [0.6%,4%]
    stop = px * (1 - stop_dist)
    risk = px - stop
    if risk <= 0:
        return None
    target = px * (1 + rr_honest_target_dist(stop_dist))  # TP scaled to net_rr 1.35 (honest)
    return build_candidate(
        strategy_id=strategy_id, strategy_version=CODE_VERSION, code_version=CODE_VERSION,
        ctx=ctx, direction=Direction.LONG, entry_type=EntryType.MARKET, timeframe=_SETUP_TF,
        reference_price=px, stop_price=stop, targets=(target,),
        expiry_ts=ctx.extras.get("expiry_ts", ctx.timestamp),
        capability_requirements=(CAP_SPOT_LONG,), min_net_rr=_MIN_NET_RR,
    )


def _short_market_candidate(strategy_id: str, ctx: StrategyContext) -> Optional[Candidate]:
    """Build a risk-sized short MARKET candidate at the live price: stop ABOVE entry, target
    BELOW. Mirror of _long_market_candidate. Returns None if uneconomic/missing features."""
    f5 = ctx.feature("5m")
    if f5 is None:
        return None
    px = float(ctx.reference_price)
    if px <= 0:
        return None
    stop_dist = atr_scaled_stop_dist(ctx, f5.atr_pct)     # 1.5x 1h-ATR, bounded [0.6%,4%]
    stop = px * (1 + stop_dist)          # short stop ABOVE entry
    risk = stop - px
    if risk <= 0:
        return None
    target = px * (1 - rr_honest_target_dist(stop_dist))  # short target BELOW entry (net_rr 1.35)
    if target <= 0:
        return None
    return build_candidate(
        strategy_id=strategy_id, strategy_version=CODE_VERSION, code_version=CODE_VERSION,
        ctx=ctx, direction=Direction.SHORT, entry_type=EntryType.MARKET, timeframe=_SETUP_TF,
        reference_price=px, stop_price=stop, targets=(target,),
        expiry_ts=ctx.extras.get("expiry_ts", ctx.timestamp),
        capability_requirements=(CAP_SHORT,), min_net_rr=_MIN_NET_RR,
    )


class C6RelativeStrengthRotation(Strategy):
    strategy_id = "RAID-C6"
    version = CODE_VERSION
    required_capabilities = frozenset({CAP_SPOT_LONG})
    eligible_regimes = frozenset({MarketRegime.TREND_UP})

    def generate_candidates(self, ctx: StrategyContext) -> list[Candidate]:
        me = _rankings(ctx).get(ctx.symbol)
        if not me or me["rank"] > C6_TOP_N:
            return []
        # Rebalance throttle (fee protection) — runner sets this False during cooldown.
        if not ctx.extras.get("c6_rebalance_ok", True):
            return []
        if _already_open(ctx):
            return []
        # Must be a real, positive, well-formed move.
        if me["return_24h"] <= 0 or me["risk_adj_momentum"] <= 0 or me["trend_quality"] < _MIN_TREND_QUALITY:
            return []
        # Confirm an uptrend on the 1h timeframe (EMA20 > EMA50).
        f1h = ctx.feature("1h")
        if f1h is None or f1h.ema20 is None or f1h.ema50 is None or not (f1h.ema20 > f1h.ema50):
            return []
        c = _long_market_candidate(self.strategy_id, ctx)
        return [c] if c else []

    def should_exit(self, position, ctx: StrategyContext) -> Optional[ExitDecision]:
        # Exits run through executor.monitor_positions in production; this keeps the
        # strategy self-consistent (relative-strength thesis dies in a down/crisis tape).
        if ctx.market_regime in (MarketRegime.TREND_DOWN, MarketRegime.CRISIS):
            return ExitDecision(True, "rotation_regime_adverse", "immediate")
        return None


class C7CrossSectionalMomentum(Strategy):
    strategy_id = "RAID-C7"
    version = CODE_VERSION
    required_capabilities = frozenset({CAP_SPOT_LONG})
    eligible_regimes = frozenset({MarketRegime.TREND_UP, MarketRegime.RANGE})

    def generate_candidates(self, ctx: StrategyContext) -> list[Candidate]:
        me = _rankings(ctx).get(ctx.symbol)
        if not me:
            return []
        n = me["n"]
        if n < 5:                              # need a real universe to form quintiles
            return []
        quintile = max(1, round(n / 5))

        # Top quintile → long winner (hold; do not re-add a name already open).
        if me["rank"] <= quintile:
            if _already_open(ctx):
                return []
            if me["return_24h"] <= 0:
                return []
            c = _long_market_candidate(self.strategy_id, ctx)
            return [c] if c else []

        # Bottom quintile → SHORT the relative laggard. Booked when the short capability is
        # granted AND the name is genuinely falling (return_24h < 0); otherwise shadow-logged
        # (don't short a name that's merely a weak member of a rising universe).
        if me["rank"] > n - quintile:
            if _already_open(ctx):
                return []                                  # hold; don't stack
            if CAP_SHORT in ctx.capabilities and me["return_24h"] < 0:
                c = _short_market_candidate(self.strategy_id, ctx)
                return [c] if c else []
            ctx.extras.setdefault("_c7_shadow_shorts", []).append({
                "symbol": ctx.symbol, "rank": me["rank"], "n": n,
                "return_24h": me["return_24h"], "risk_adj_momentum": me["risk_adj_momentum"],
            })
            return []

        return []  # middle of the pack — no action

    def should_exit(self, position, ctx: StrategyContext) -> Optional[ExitDecision]:
        if ctx.market_regime == MarketRegime.CRISIS:
            return ExitDecision(True, "momentum_crisis", "immediate")
        return None
