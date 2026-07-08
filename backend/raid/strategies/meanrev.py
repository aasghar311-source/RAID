"""RAID-C4 — Range Mean Reversion. Buys near a validated range low in a RANGE regime
with a leaning-oversold RSI, targeting the range mid/high. Strict invalidation just
below support; no averaging down. Long sleeve is paper; shorts are shadow-only."""

from __future__ import annotations

from typing import Optional

import config
from raid.core.candidate import Candidate, Direction, EntryType, MarketRegime
from raid.core.provider import CAP_SPOT_LONG
from raid.core.strategy import ExitDecision, Strategy, StrategyContext
from raid.strategies.helpers import build_candidate

CODE_VERSION = "omega-0.1.0"
_TF = "5m"
_MIN_NET_RR = 1.20

# Range-entry gates. RSI ceiling at the §2 spec (45, leaning oversold). NOTE: C4 is SHADOW
# (config.STRATEGY_SHADOW) and structurally benched — it books nothing and fires ~0 on the liquid
# universe (see the _BAND_MIN note below). The position gate (lower third) and band width are anchored
# to the range low while the runner books at reference_price, so the RSI ceiling — not the position/
# band — is the lever if C4 is ever reactivated. A target>px guard prevents a booked entry from
# sitting at/above its own target. (The 2026-07-03 "124-trade review" loosening to 50 is reverted —
# it was pre-Stage-D and inert once C4 went shadow; kept honest to spec.)
# _BAND_MIN RECALIBRATED for the liquid universe (harness): liquid ranges are TIGHT (median 1.33%,
# p90 2.6%), and C4 reverts to the range MID (reward ~ band/2), so a band < ~6.9% gives reward < the
# 1.04% round-trip cost and net_rr correctly REJECTS it. So the economic floor is ~6.9% -> only the
# rare very-wide liquid range qualifies. FLAG: range mean-reversion is a STRUCTURALLY MARGINAL fit for
# the liquid universe (tight ranges); C4 fires rarely by design, not by tuning. Was 0.01 (alt-tuned).
_BAND_MIN = 0.069
_BAND_MAX = 0.15             # was 0.10; raised so the (now-rare) wide liquid range isn't clipped
_RANGE_POSITION_MAX = 0.33   # fraction of range height above the low (UNCHANGED)
_RSI_MAX = 45                # §2 spec ceiling (leaning oversold); reverted from the inert 50 loosening


class C4RangeMeanReversion(Strategy):
    strategy_id = "RAID-C4"
    version = CODE_VERSION
    required_capabilities = frozenset({CAP_SPOT_LONG})
    eligible_regimes = frozenset()   # Stage-D: gated by the SPINE + C4's own range detection

    def generate_candidates(self, ctx: StrategyContext) -> list[Candidate]:
        # Stage-D: a range-low dip is a LONG — never fire on a down-trending pair (spine SHORT) or in
        # a RISK_OFF/CRISIS book (a range low there is likely breaking down, not reverting).
        if ctx.extras.get("spine_dir") == "SHORT" or \
                ctx.extras.get("spine_portfolio") in ("RISK_OFF", "CRISIS", "UNKNOWN"):
            return []
        f = ctx.feature(_TF)
        if f is None or f.swing_low is None or f.swing_high is None or f.rsi14 is None:
            return []
        # §10 range MINIMUM (completed-bar) — reject only DEAD-volume ranges (a dip, not a breakout).
        vrc = ctx.extras.get("vol_ratio_completed")
        if vrc is None or vrc < config.C4_VOLUME_MULT:
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
        if f.rsi14 > _RSI_MAX:                    # leaning oversold (ceiling at §2 spec 45)
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
