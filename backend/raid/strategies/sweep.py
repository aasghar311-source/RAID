"""RAID-C10 — Liquidity Sweep Reversal.

Trades a VALIDATED liquidity sweep: an abnormal displacement wick that grabs liquidity
beyond a recent swing level and then rejects, confirmed by a volume spike and an
imbalanced order book (see raid.core.microstructure.detect_liquidity_sweep). The runner
threads the raw 5m candles + order book into ctx.extras.

Long sweeps (bullish reversals off a swept low) are PAPER. Short sweeps are detected
and logged for dashboard visibility but stay SHADOW — no shorts until margin is
operator-enabled. Sweeps resolve fast, so C10 positions get a 90-minute time stop,
enforced for RAID-C10-tagged trades in executor.monitor_positions (the runner's exit path).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from raid.core.candidate import Candidate, Direction, EntryType, MarketRegime
from raid.core.microstructure import detect_liquidity_sweep
from raid.core.provider import CAP_SPOT_LONG
from raid.core.strategy import ExitDecision, Strategy, StrategyContext
from raid.execution.time_stops import C10_MAX_HOLD_MINUTES as MAX_HOLD_MINUTES
from raid.strategies.helpers import build_candidate

CODE_VERSION = "omega-0.2.0"
_TF = "5m"
_MIN_NET_RR = 1.20
_TP_DEPTH_MULT = 2.2       # target ~= 2x sweep depth (2.2x to clear round-trip costs)
_STOP_BUFFER = 0.001       # stop just beyond the swept wick (the liquidity-grab level)
# MAX_HOLD_MINUTES is imported from raid.execution.time_stops (single source of truth,
# also enforced in executor.monitor_positions — the production exit path).


class C10LiquiditySweepReversal(Strategy):
    strategy_id = "RAID-C10"
    version = CODE_VERSION
    required_capabilities = frozenset({CAP_SPOT_LONG})
    eligible_regimes = frozenset({MarketRegime.VOLATILE, MarketRegime.CRISIS})

    @staticmethod
    def sweep_tradeable(direction: str) -> bool:
        """Long sweeps are paper-tradeable; short sweeps stay shadow (need margin)."""
        return direction == "long"

    def generate_candidates(self, ctx: StrategyContext) -> list[Candidate]:
        sweep = detect_liquidity_sweep(ctx.extras.get("candles_5m"), ctx.extras.get("order_book"))
        if sweep is None:
            return []

        # Log EVERY detected sweep (traded or not) for dashboard/data-quality visibility.
        ctx.extras.setdefault("_c10_sweeps", []).append({"symbol": ctx.symbol, **sweep})

        if not self.sweep_tradeable(sweep["direction"]):
            ctx.extras.setdefault("_c10_shadow", []).append({"symbol": ctx.symbol, **sweep})
            return []
        if ctx.symbol in (ctx.extras.get("open_symbols") or set()):
            return []

        px = float(ctx.reference_price)
        wick_low = float(sweep["wick_low"])
        depth = px - wick_low
        if px <= 0 or wick_low <= 0 or depth <= 0:
            return []
        stop = wick_low * (1 - _STOP_BUFFER)
        if stop >= px:
            return []
        target = px + _TP_DEPTH_MULT * depth
        c = build_candidate(
            strategy_id=self.strategy_id, strategy_version=self.version, code_version=CODE_VERSION,
            ctx=ctx, direction=Direction.LONG, entry_type=EntryType.MARKET, timeframe=_TF,
            reference_price=px, stop_price=stop, targets=(target,),
            expiry_ts=ctx.extras.get("expiry_ts", ctx.timestamp),
            capability_requirements=(CAP_SPOT_LONG,), min_net_rr=_MIN_NET_RR,
        )
        return [c] if c else []

    def should_exit(self, position, ctx: StrategyContext) -> Optional[ExitDecision]:
        """Fast time stop (90m). Enforcement in production is the RAID-C10 branch in
        executor.monitor_positions; this keeps the strategy self-consistent + testable."""
        if ctx.market_regime == MarketRegime.CRISIS:
            # A sweep that decays straight into crisis was not the clean reversal we want.
            pass
        opened = position.get("open_time") if isinstance(position, dict) else None
        if opened:
            try:
                ot = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
                if ot.tzinfo is None:
                    ot = ot.replace(tzinfo=timezone.utc)
                mins = (datetime.now(timezone.utc) - ot).total_seconds() / 60.0
                if mins >= MAX_HOLD_MINUTES:
                    return ExitDecision(True, "sweep_time_stop", "normal")
            except (ValueError, TypeError):
                return None
        return None
