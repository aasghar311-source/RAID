"""Tests for the promotion / quarantine evidence engine."""

from raid.core.promotion import (
    PromotionCriteria, StrategyEvidence, evaluate_promotion, evaluate_quarantine, next_mode,
)
from raid.core.strategy import StrategyMode


def _good(**kw) -> StrategyEvidence:
    base = dict(
        strategy_id="RAID-C1", trades=60, net_expectancy=0.12, profit_factor=1.5,
        max_drawdown_pct=0.08, largest_trade_pnl_share=0.15, largest_symbol_trade_share=0.30,
        distinct_periods=3, out_of_sample_positive=True, shadow_paper_divergence=0.10,
        critical_errors=0,
    )
    base.update(kw)
    return StrategyEvidence(**base)


def test_promote_when_all_gates_pass():
    d = evaluate_promotion(_good())
    assert d.promote is True
    assert not d.failed


def test_no_promote_on_small_sample():
    d = evaluate_promotion(_good(trades=10))
    assert d.promote is False and "min_trades" in d.failed


def test_no_promote_on_single_trade_domination():
    d = evaluate_promotion(_good(largest_trade_pnl_share=0.60))
    assert d.promote is False and "not_one_trade_dominated" in d.failed


def test_no_promote_on_single_period():
    d = evaluate_promotion(_good(distinct_periods=1))
    assert "multiple_periods" in d.failed


def test_no_promote_without_out_of_sample():
    assert not evaluate_promotion(_good(out_of_sample_positive=False)).promote


def test_no_promote_on_shadow_paper_divergence():
    assert "shadow_paper_consistent" in evaluate_promotion(_good(shadow_paper_divergence=0.5)).failed


def test_quarantine_on_degradation():
    assert evaluate_quarantine(_good(critical_errors=2)).quarantine
    assert evaluate_quarantine(_good(net_expectancy=-0.05)).quarantine
    assert evaluate_quarantine(_good(max_drawdown_pct=0.20)).quarantine
    assert not evaluate_quarantine(_good()).quarantine


def test_next_mode_transitions():
    # shadow + strong evidence -> paper
    assert next_mode(StrategyMode.SHADOW, _good()) == StrategyMode.PAPER
    # paper + degradation -> quarantined
    assert next_mode(StrategyMode.PAPER, _good(net_expectancy=-0.1)) == StrategyMode.QUARANTINED
    # shadow + weak evidence -> stays shadow
    assert next_mode(StrategyMode.SHADOW, _good(trades=5)) == StrategyMode.SHADOW
    # disabled never auto-promotes
    assert next_mode(StrategyMode.DISABLED, _good()) == StrategyMode.DISABLED
    # quarantine dominates promotion even if other gates pass
    assert next_mode(StrategyMode.SHADOW, _good(critical_errors=1)) == StrategyMode.QUARANTINED
