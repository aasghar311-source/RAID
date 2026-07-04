"""ATR-scaled stop + RR-honest TP tests.

Replaces the flat ~1% stop on the ATR strategies (C1/C3/C6/C7/C5) with 1.5x the 1h ATR%(14),
bounded [0.6%, 4%]; the TP scales off the per-pair stop to keep net_rr == RR_TARGET_NET (1.35)
after the real 1.04% round-trip cost. Structural strategies (C2 swing, C4 range, C10 sweep) are
untouched.

Discovered and run by raid.tests.run_all (plain asserts, no pytest).
"""

import config
import costs
from raid.strategies.helpers import atr_scaled_stop_dist, rr_honest_target_dist

TOL = 1e-12


class _F:
    def __init__(self, atr):
        self.atr_pct = atr


class _Ctx:
    def __init__(self, atr1h):
        self._a = atr1h

    def feature(self, tf):
        return _F(self._a) if tf == "1h" else None


def test_stop_is_1_5x_1h_atr_bounded():
    assert abs(atr_scaled_stop_dist(_Ctx(0.024)) - 0.036) < TOL   # wild 2.4% ATR -> 3.6% stop
    assert abs(atr_scaled_stop_dist(_Ctx(0.006)) - 0.009) < TOL   # 0.6% ATR -> 0.9%
    assert abs(atr_scaled_stop_dist(_Ctx(0.001)) - config.ATR_STOP_MIN) < TOL   # floor 0.6%
    assert abs(atr_scaled_stop_dist(_Ctx(0.050)) - config.ATR_STOP_MAX) < TOL   # ceiling 4%


def test_wild_pair_stop_now_clears_1x_atr_noise():
    # The whole point: a 2.4%-ATR pair's stop (3.6%) is now OUTSIDE 1x its normal candle,
    # where the old flat 1% stop sat far INSIDE the noise.
    atr = 0.024
    assert atr_scaled_stop_dist(_Ctx(atr)) > atr        # stop > 1x ATR
    assert atr_scaled_stop_dist(_Ctx(atr)) > 0.01       # and wider than the old flat 1%


def test_tp_holds_net_rr_target_across_atr():
    c = costs.realized_round_trip_cost_pct()
    for stop in (0.006, 0.009, 0.02, 0.036, 0.04):
        tp = rr_honest_target_dist(stop)
        net_rr = (tp - c) / (stop + c)
        assert abs(net_rr - config.RR_TARGET_NET) < 1e-9, (stop, net_rr)
        assert net_rr >= 1.20                            # honest gate always cleared


def test_fallback_when_no_1h_feature():
    class _NoCtx:
        def feature(self, tf):
            return None
    assert abs(atr_scaled_stop_dist(_NoCtx(), 0.02) - 0.03) < TOL   # uses signal-TF fallback
    assert abs(atr_scaled_stop_dist(_NoCtx(), None) - config.ATR_STOP_MIN) < TOL  # floor when nothing


def test_trigger_lock_and_fee_untouched():
    # KEEP-INTACT: this task changes only stop/TP construction.
    assert config.TRAIL_TRIGGER_PCT == 0.015
    assert abs(costs.realized_round_trip_cost_pct() - 0.0104) < 1e-9
