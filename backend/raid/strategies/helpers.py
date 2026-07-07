"""Shared candidate construction — wires the authoritative cost model (costs.py) and
the risk-based position sizer into a single validated Candidate.

A strategy proposes STRUCTURE (entry/stop/targets); this helper computes net economics
after real costs and sizes the position from the risk budget in the context. If the
setup is uneconomic or degenerate it raises, and the strategy takes the no-trade path
(it never widens a target or repairs a stop to force a pass).
"""

from __future__ import annotations

from decimal import Decimal

import config
import costs  # backend/costs.py — authoritative cost model
from raid.core.candidate import Candidate, Direction, EntryType, MarketRegime
from raid.core.features import volume_ratio
from raid.core.risk import position_size
from raid.core.strategy import StrategyContext


def atr_scaled_stop_dist(ctx: StrategyContext, fallback_atr_pct=None) -> float:
    """Stop distance (fraction of price) = ATR_STOP_MULT (1.5) x the 1h ATR%(14), bounded to
    [ATR_STOP_MIN, ATR_STOP_MAX] ([0.6%, 4%]). The 1h ATR is the noise a stop must survive over a
    multi-hour hold — the old flat ~1% stop was tighter than a normal 1h candle on volatile pairs.
    Falls back to the signal-TF ATR, then the floor, if the 1h feature is missing (fail safe)."""
    f1h = ctx.feature("1h")
    atr = (f1h.atr_pct if (f1h is not None and f1h.atr_pct) else fallback_atr_pct) or 0.0
    # clamp handles the no-ATR case: 1.5*0 -> max(_, MIN) = the 0.6% floor.
    return min(max(config.ATR_STOP_MULT * float(atr), config.ATR_STOP_MIN), config.ATR_STOP_MAX)


def rr_honest_target_dist(stop_dist: float, target_net_rr: float | None = None) -> float:
    """TP distance (fraction) that makes net_rr == target after the REAL round-trip cost, given
    the stop: (tp - c)/(stop + c) = target  ->  tp = target*(stop + c) + c. Keeps the gate honest
    (>= min_net_rr) while the stop/TP distances scale per-pair with ATR. Default target = 1.35."""
    c = costs.realized_round_trip_cost_pct()
    tgt = config.RR_TARGET_NET if target_net_rr is None else target_net_rr
    return tgt * (float(stop_dist) + c) + c


def _d(x) -> Decimal:
    return Decimal(str(x))


def build_candidate(
    *,
    strategy_id: str,
    strategy_version: str,
    code_version: str,
    ctx: StrategyContext,
    direction: Direction,
    entry_type: EntryType,
    timeframe: str,
    reference_price: float,
    stop_price: float,
    targets: tuple[float, ...],
    trigger_price: float | None = None,
    limit_price: float | None = None,
    expiry_ts: str,
    fee_pct: float | None = None,
    capability_requirements: tuple[str, ...] = (),
    min_net_rr: float = 1.25,
) -> Candidate | None:
    """Return a validated Candidate, or None if uneconomic / unsizeable.

    equity and risk_pct are read from ctx.extras ('equity', 'risk_pct'); a strategy
    running in SHADOW with no equity simply produces structure sized off a nominal
    equity so its scorecard can still be computed.
    """
    # FAIL-CLOSED hard-zero volume gate (shared — every strategy routes through here). A 5m bar
    # with no traded volume has no real market, so a fill on it is fiction; reject when the latest
    # 5m bar volume is 0, or volume is missing/uncomputable (insufficient bars / dead pair). Reuses
    # the engine's own volume_ratio (None on missing/insufficient, 0.0 on a zero-volume latest bar).
    # MIN_VOLUME_RATIO=0.0 blocks ONLY zero/missing today — a genuine small positive ratio passes;
    # raise it later to test a thin-volume threshold. HARD-ZERO ONLY; no other entry condition changed.
    _vr = volume_ratio(ctx.extras.get("candles_5m"))
    if _vr is None or _vr <= config.MIN_VOLUME_RATIO:
        return None

    entry = (
        limit_price if entry_type == EntryType.LIMIT and limit_price else
        trigger_price if entry_type == EntryType.STOP and trigger_price else
        reference_price
    )
    if entry <= 0 or stop_price <= 0 or not targets:
        return None

    is_long = direction == Direction.LONG
    t0 = targets[0]
    gross_reward = abs(t0 - entry) / entry
    gross_risk = abs(entry - stop_price) / entry
    if gross_risk <= 0 or gross_reward <= 0:
        return None

    # HONEST GATE: net_rr uses the SAME all-in realized round-trip cost as P&L
    # (costs.realized_round_trip_cost_pct ~1.04% — the SSOT), not the old 0.16% maker
    # assumption. The 1%/4% geometry clears min_net_rr 1.20 at this cost (~1.45).
    fp = costs.KRAKEN_TAKER_FEE_PCT if fee_pct is None else fee_pct   # taker — the engine's fill side
    spread = float(ctx.spread_pct or 0.0)                            # book spread (audit metadata)
    rt_cost = costs.realized_round_trip_cost_pct()
    net_reward = gross_reward - rt_cost
    net_risk = gross_risk + rt_cost
    if net_risk <= 0 or net_reward <= 0:
        return None
    net_rr = net_reward / net_risk
    if net_rr < min_net_rr:
        return None  # uneconomic after costs — reject, never widen

    equity = _d(ctx.extras.get("equity", 4000.0))
    risk_pct = float(ctx.extras.get("risk_pct", 0.005))
    try:
        risk_dollars, quantity = position_size(equity, risk_pct, _d(entry), _d(stop_price))
    except ValueError:
        return None

    try:
        return Candidate(
            candidate_id=f"{strategy_id}:{ctx.symbol}:{ctx.timestamp}",
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            code_version=code_version,
            symbol=ctx.symbol,
            instrument_id=ctx.instrument_id,
            direction=direction,
            setup_timeframe=timeframe,
            market_regime=ctx.market_regime,
            entry_type=entry_type,
            trigger_price=_d(trigger_price) if trigger_price else None,
            limit_price=_d(limit_price) if limit_price else None,
            reference_price=_d(reference_price),
            stop_price=_d(stop_price),
            targets=tuple(_d(t) for t in targets),
            expected_entry_fee=_d(fp),
            expected_exit_fee=_d(fp),
            expected_spread=_d(spread),
            expected_slippage=_d(0),
            gross_reward=_d(round(gross_reward, 8)),
            gross_risk=_d(round(gross_risk, 8)),
            net_reward=_d(round(net_reward, 8)),
            net_risk=_d(round(net_risk, 8)),
            net_rr=_d(round(net_reward / net_risk, 4)),
            planned_risk_dollars=risk_dollars,
            quantity=quantity,
            setup_timestamp=ctx.timestamp,
            expiry_timestamp=expiry_ts,
            market_data_snapshot_id=ctx.market_data_snapshot_id,
            feature_snapshot_id=(ctx.feature(timeframe).snapshot_id if ctx.feature(timeframe) else "none"),
            capability_requirements=capability_requirements,
        )
    except Exception:
        # Structural validation failed (wrong-side stop/target, etc.) -> no-trade.
        return None
