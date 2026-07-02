"""Tests for the expectancy-weighted capital allocator."""

from raid.core.allocator import (
    StrategyStats, allocate, MIN_SAMPLE, MAX_WEIGHT_PER_STRATEGY,
)


def test_all_cash_when_none_eligible():
    stats = [
        StrategyStats("A", sample_size=5, net_expectancy=1.0, profit_factor=2.0, max_drawdown_pct=0.1),   # too few
        StrategyStats("B", sample_size=100, net_expectancy=-0.2, profit_factor=0.8, max_drawdown_pct=0.1),  # negative
    ]
    a = allocate(stats)
    assert a.weights == {}
    assert a.cash_weight == 1.0


def test_positive_expectancy_gets_weight():
    stats = [
        StrategyStats("A", 100, 0.50, 1.5, 0.10),
        StrategyStats("B", 100, 0.20, 1.2, 0.10),
    ]
    a = allocate(stats)
    assert "A" in a.weights and "B" in a.weights
    # A has higher expectancy -> higher weight
    assert a.weights["A"] > a.weights["B"]
    # Weights + cash conserve to 1
    assert abs(sum(a.weights.values()) + a.cash_weight - 1.0) < 1e-9


def test_min_sample_gate():
    stats = [StrategyStats("A", MIN_SAMPLE - 1, 5.0, 3.0, 0.0)]
    assert allocate(stats).cash_weight == 1.0


def test_shrinkage_penalizes_small_samples():
    small = [StrategyStats("S", 25, 1.0, 1.5, 0.1), StrategyStats("L", 500, 1.0, 1.5, 0.1)]
    a = allocate(small)
    # Same expectancy/pf/dd but L has far more samples -> larger shrunk score -> more weight
    assert a.weights["L"] > a.weights["S"]


def test_per_strategy_cap():
    # One dominant strategy should be capped, overflow to cash.
    stats = [
        StrategyStats("A", 500, 10.0, 3.0, 0.0),
        StrategyStats("B", 30, 0.05, 1.05, 0.0),
    ]
    a = allocate(stats)
    assert a.weights["A"] <= MAX_WEIGHT_PER_STRATEGY + 1e-9
    assert a.cash_weight >= 0.0


def test_drawdown_penalty_zeroes_out():
    # 50%+ drawdown -> penalty drives score to 0 -> no allocation
    stats = [StrategyStats("A", 100, 1.0, 2.0, 0.60)]
    assert allocate(stats).cash_weight == 1.0
