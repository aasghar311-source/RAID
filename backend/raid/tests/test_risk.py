"""Tests for the portfolio risk manager, tiers, and sizing."""

from decimal import Decimal

from raid.core.risk import (
    RiskTier, TIER_LIMITS, HARD_CEILING_PCT, effective_tier, clamped_risk_pct,
    position_size, PortfolioState, PortfolioRiskManager,
)


def test_tier_limits_and_ceiling():
    assert TIER_LIMITS[RiskTier.INITIAL].risk_per_trade_pct == 0.0050
    assert TIER_LIMITS[RiskTier.EXCEPTIONAL].risk_per_trade_pct == 0.0150
    # No tier exceeds the hard ceiling.
    for t in RiskTier:
        assert clamped_risk_pct(t) <= HARD_CEILING_PCT + 1e-12


def test_effective_tier_derisk_ladder():
    assert effective_tier(RiskTier.STRONG, 0.0) == RiskTier.STRONG
    assert effective_tier(RiskTier.STRONG, 0.07) == RiskTier.VALIDATED   # one tier down
    assert effective_tier(RiskTier.AGGRESSIVE, 0.11) == RiskTier.INITIAL  # to tier 1
    assert effective_tier(RiskTier.INITIAL, 0.07) == RiskTier.INITIAL     # cannot go below 1 here


def test_position_size_math():
    rd, qty = position_size(Decimal("10000"), 0.01, Decimal("100"), Decimal("99"))
    assert rd == Decimal("100")     # 1% of 10k
    assert qty == Decimal("100")    # notional 10000 / entry 100
    # Degenerate stop -> fail closed.
    try:
        position_size(Decimal("10000"), 0.01, Decimal("100"), Decimal("100"))
        raise AssertionError("expected ValueError on zero stop distance")
    except ValueError:
        pass


def test_halt_conditions():
    m = PortfolioRiskManager(RiskTier.STRONG)
    assert m.system_halted(PortfolioState(Decimal("8000"), Decimal("10000"))) is not None  # 20% dd -> shutdown
    assert m.system_halted(PortfolioState(Decimal("8500"), Decimal("10000"))) is not None  # 15% dd -> pause entries
    assert m.system_halted(PortfolioState(Decimal("8600"), Decimal("10000"))) is None      # 14% dd -> below pause
    # daily loss 4% halts
    st_daily = PortfolioState(Decimal("10000"), Decimal("10000"), daily_loss_pct=0.04)
    assert m.system_halted(st_daily) is not None
    st_weekly = PortfolioState(Decimal("10000"), Decimal("10000"), weekly_loss_pct=0.08)
    assert m.system_halted(st_weekly) is not None
    assert m.system_halted(PortfolioState(Decimal("10000"), Decimal("10000"))) is None


def test_assess_approves_and_sizes():
    m = PortfolioRiskManager(RiskTier.STRONG)
    st = PortfolioState(Decimal("10000"), Decimal("10000"))
    d = m.assess(st, Decimal("100"), Decimal("99"))
    assert d.approved is True
    assert d.effective_tier == RiskTier.STRONG
    assert d.risk_dollars == Decimal("100")   # 1% tier
    assert d.quantity == Decimal("100")


def test_assess_blocks_on_shutdown_and_exposure():
    m = PortfolioRiskManager(RiskTier.STRONG)
    # 20% drawdown -> hard shutdown
    d = m.assess(PortfolioState(Decimal("8000"), Decimal("10000")), Decimal("100"), Decimal("99"))
    assert d.approved is False and "shutdown" in d.reason
    # total open risk already maxed for STRONG (5%)
    st = PortfolioState(Decimal("10000"), Decimal("10000"), open_risk_pct=0.05)
    d2 = m.assess(st, Decimal("100"), Decimal("99"))
    assert d2.approved is False and "max_total_open_risk" in d2.reason
    # cluster maxed (2.5% for STRONG)
    st3 = PortfolioState(Decimal("10000"), Decimal("10000"), cluster_risk_pct=0.025)
    d3 = m.assess(st3, Decimal("100"), Decimal("99"))
    assert d3.approved is False and "cluster" in d3.reason


def test_shadow_tier_no_capital():
    m = PortfolioRiskManager(RiskTier.SHADOW)
    d = m.assess(PortfolioState(Decimal("10000"), Decimal("10000")), Decimal("100"), Decimal("99"))
    assert d.approved is False
