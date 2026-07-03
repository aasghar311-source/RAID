"""Strategy promotion & quarantine engine (Section 12).

Deterministic, evidence-gated transitions between SHADOW and PAPER. Every gate in
Section 12 must pass for promotion; any single degradation trigger quarantines. No
strategy is promoted on a short lucky streak, and quarantine never deletes history.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from raid.core.strategy import StrategyMode


@dataclass(frozen=True)
class PromotionCriteria:
    min_trades: int = 40
    min_net_expectancy: float = 0.0          # must be > this (strictly positive)
    min_profit_factor: float = 1.20
    max_drawdown_pct: float = 0.15
    max_single_trade_pnl_share: float = 0.35  # not dominated by one trade
    max_single_symbol_trade_share: float = 0.50  # not dominated by one symbol
    min_distinct_periods: int = 2             # more than one market period
    require_out_of_sample: bool = True
    max_shadow_paper_divergence: float = 0.25  # |shadow-paper| expectancy divergence


@dataclass(frozen=True)
class StrategyEvidence:
    strategy_id: str
    trades: int
    net_expectancy: float
    profit_factor: float
    max_drawdown_pct: float
    largest_trade_pnl_share: float           # 0..1, |biggest trade| / gross
    largest_symbol_trade_share: float        # 0..1, trades in top symbol / trades
    distinct_periods: int
    out_of_sample_positive: bool
    shadow_paper_divergence: float           # 0..1
    critical_errors: int = 0


@dataclass(frozen=True)
class PromotionDecision:
    promote: bool
    passed: tuple[str, ...]
    failed: tuple[str, ...]


@dataclass(frozen=True)
class QuarantineDecision:
    quarantine: bool
    reasons: tuple[str, ...]


def evaluate_promotion(ev: StrategyEvidence, c: PromotionCriteria = PromotionCriteria()) -> PromotionDecision:
    passed: list[str] = []
    failed: list[str] = []

    def gate(name: str, ok: bool):
        (passed if ok else failed).append(name)

    gate("min_trades", ev.trades >= c.min_trades)
    gate("positive_expectancy", ev.net_expectancy > c.min_net_expectancy)
    gate("profit_factor", ev.profit_factor >= c.min_profit_factor)
    gate("controlled_drawdown", ev.max_drawdown_pct <= c.max_drawdown_pct)
    gate("stable_execution", ev.critical_errors == 0)
    gate("not_one_trade_dominated", ev.largest_trade_pnl_share <= c.max_single_trade_pnl_share)
    gate("not_one_symbol_dominated", ev.largest_symbol_trade_share <= c.max_single_symbol_trade_share)
    gate("multiple_periods", ev.distinct_periods >= c.min_distinct_periods)
    if c.require_out_of_sample:
        gate("out_of_sample_positive", ev.out_of_sample_positive)
    gate("shadow_paper_consistent", ev.shadow_paper_divergence <= c.max_shadow_paper_divergence)

    return PromotionDecision(promote=not failed, passed=tuple(passed), failed=tuple(failed))


def evaluate_quarantine(ev: StrategyEvidence, c: PromotionCriteria = PromotionCriteria()) -> QuarantineDecision:
    """A live (PAPER) strategy is quarantined on ANY degradation trigger."""
    reasons: list[str] = []
    if ev.critical_errors > 0:
        reasons.append(f"critical_errors={ev.critical_errors}")
    if ev.trades >= c.min_trades and ev.net_expectancy <= 0:
        reasons.append("expectancy_turned_nonpositive")
    if ev.max_drawdown_pct > c.max_drawdown_pct:
        reasons.append(f"drawdown_breach={ev.max_drawdown_pct:.3f}")
    if ev.profit_factor < 1.0 and ev.trades >= c.min_trades:
        reasons.append(f"profit_factor_below_1={ev.profit_factor:.2f}")
    return QuarantineDecision(quarantine=bool(reasons), reasons=tuple(reasons))


def next_mode(current: StrategyMode, ev: StrategyEvidence,
              c: PromotionCriteria = PromotionCriteria()) -> StrategyMode:
    """Resolve the target mode from evidence. Never promotes a DISABLED strategy;
    quarantine dominates promotion."""
    if current == StrategyMode.DISABLED:
        return current
    q = evaluate_quarantine(ev, c)
    if q.quarantine:
        return StrategyMode.QUARANTINED
    if current in (StrategyMode.SHADOW, StrategyMode.QUARANTINED):
        if evaluate_promotion(ev, c).promote:
            return StrategyMode.PAPER
        # a quarantined strategy that no longer degrades but isn't promotable returns to shadow
        return StrategyMode.SHADOW
    return current
