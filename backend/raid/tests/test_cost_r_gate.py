"""Commit 2 — graduated cost/R gate. Pure decision math + strategy scoping. Plain asserts."""

import config
import costs
from raid.core.risk import graduated_size_decision

RT = costs.realized_round_trip_cost_pct()   # ~0.0104 (SSOT)


def _decide(gross_risk):
    return graduated_size_decision(
        gross_risk, RT,
        fatal_ratio=config.COST_R_FATAL_RATIO,
        marginal_ratio=config.COST_R_MARGINAL_RATIO,
        marginal_mult=config.COST_R_MARGINAL_SIZE_MULT,
    )


def test_fatal_band_rejected():
    # 0.8% stop -> cost/R = 0.0104/0.008 = 1.30 >= 0.87 -> reject, no size
    allow, mult, _ = _decide(0.008)
    assert allow is False and mult == 0.0
    # a floored 0.6% ATR stop is also fatal
    assert _decide(0.006)[0] is False


def test_marginal_band_half_size():
    # 1.3% stop -> cost/R = 0.80, in [0.69, 0.87) -> allow at half size
    allow, mult, _ = _decide(0.013)
    assert allow is True and mult == config.COST_R_MARGINAL_SIZE_MULT == 0.5


def test_full_size_band():
    # 2.0% stop -> cost/R = 0.52 < 0.69 -> full size
    allow, mult, _ = _decide(0.020)
    assert allow is True and mult == 1.0


def test_boundaries_align_with_derived_thresholds():
    fatal_stop = RT / config.COST_R_FATAL_RATIO        # ~1.195%
    marginal_stop = RT / config.COST_R_MARGINAL_RATIO  # ~1.507%
    assert _decide(fatal_stop - 1e-4)[0] is False               # just inside fatal -> reject
    assert _decide(fatal_stop + 1e-4) == (True, 0.5, _decide(fatal_stop + 1e-4)[2])  # marginal
    assert _decide(marginal_stop + 1e-4)[1] == 1.0              # just past marginal -> full
    # sanity: derived stops correspond to ~0.80% / ~1.00% 1h-ATR (stop = 1.5x ATR)
    assert abs(fatal_stop / 1.5 - 0.008) < 5e-4
    assert abs(marginal_stop / 1.5 - 0.010) < 5e-4


def test_degenerate_stop_rejected():
    assert _decide(0.0)[0] is False
    assert _decide(-0.01)[0] is False


def test_gate_scoped_to_atr_strategies_only():
    from raid.strategies.trend import C1LongTrendBreakout, C2LongTrendPullback, C3ShortTrendBreakdown
    from raid.strategies.volatility import C5VolatilityExpansion
    from raid.strategies.meanrev import C4RangeMeanReversion
    from raid.strategies.sweep import C10LiquiditySweepReversal
    from raid.strategies.rotation import C6RelativeStrengthRotation, C7CrossSectionalMomentum
    atr = [C1LongTrendBreakout, C3ShortTrendBreakdown, C5VolatilityExpansion,
           C6RelativeStrengthRotation, C7CrossSectionalMomentum]
    structural = [C2LongTrendPullback, C4RangeMeanReversion, C10LiquiditySweepReversal]
    for s in atr:
        assert s.atr_scaled_stop is True, s.strategy_id
    for s in structural:
        assert s.atr_scaled_stop is False, s.strategy_id
