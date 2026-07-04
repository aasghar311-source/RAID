"""RAID-C4 — Range Mean Reversion. Buys near a validated range low in a RANGE regime
with a leaning-oversold RSI, targeting the range mid/high. Strict invalidation just
below support; no averaging down. Long sleeve is paper; shorts are shadow-only."""

from __future__ import annotations

import logging
from typing import Optional

from raid.core.candidate import Candidate, Direction, EntryType, MarketRegime
from raid.core.provider import CAP_SPOT_LONG
from raid.core.strategy import ExitDecision, Strategy, StrategyContext
from raid.strategies.helpers import build_candidate

log = logging.getLogger("raid.strategies.c4")

CODE_VERSION = "omega-0.1.0"
_TF = "5m"
_MIN_NET_RR = 1.20

# Range-entry gates. The RSI ceiling was LOOSENED 2026-07-03 per the 124-trade review:
# C4 was 88.9% win / +$1.60 per trade but fired only 9x while ~80% of the market was in
# RANGE. The position gate (lower third) and band width are LEFT UNCHANGED on purpose —
# the runner books at the current price (reference_price) while C4's stop/target are
# anchored to the range low, so widening those would admit trades whose booked entry
# drifts far above the low (poor real geometry, or a degenerate tp<=entry), diluting C4's
# edge. The safe lever is the RSI ceiling: it admits near-low entries on a neutral (not
# deeply oversold) RSI while keeping the healthy near-low geometry. A target>px guard is
# added so a booked entry can never sit at/above its own target.
_BAND_MIN = 0.01
_BAND_MAX = 0.10
_RANGE_POSITION_MAX = 0.33   # fraction of range height above the low (UNCHANGED)
_RSI_MAX = 50                # was 45 — loosened per 124-trade review

log.info("C4 config: RSI ceiling 45 -> %d (loosened per 124-trade review); "
         "position %.2f / band [%.2f, %.2f] unchanged (reference-price booking)",
         _RSI_MAX, _RANGE_POSITION_MAX, _BAND_MIN, _BAND_MAX)


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
        if band < _BAND_MIN or band > _BAND_MAX:   # too tight to trade / too wide = not a range
            return []
        if px > lo + _RANGE_POSITION_MAX * (hi - lo):   # only near the low
            return []
        if f.rsi14 > _RSI_MAX:                    # leaning oversold (ceiling loosened 45 -> 50)
            return []
        limit = lo * 1.002                       # bid just above support
        stop = lo * (1 - 0.006)                  # invalidation below support
        if stop >= limit:
            return []
        mid = (hi + lo) / 2
        # Reversion target = the range mid (capped +6%). The old +3% cap truncated the mid
        # for wide ranges and made C4 uneconomic under the honest 1.04% gate; targeting the
        # true mid lets wide-range reversions clear it (narrow ranges stay honestly gated).
        target = min(mid, limit * 1.06)
        if target <= px:                         # runner books at px — require real upside (no tp<=entry)
            return []
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
