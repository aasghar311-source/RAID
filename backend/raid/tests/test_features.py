"""Tests for the deterministic feature engine."""

from raid.core import features as F


def test_sma_and_ema():
    assert F.sma([1, 2, 3, 4], 2) == 3.5
    # ema period 3 on [1,2,3,4,5]: seed=2, ->3, ->4
    assert F.ema([1, 2, 3, 4, 5], 3) == 4.0
    assert F.sma([1, 2], 5) is None
    assert F.ema([1, 2], 5) is None


def test_rsi_extremes():
    assert F.rsi(list(range(1, 21)), 14) == 100.0        # all gains
    assert F.rsi(list(range(20, 0, -1)), 14) == 0.0      # all losses
    assert F.rsi([1, 2, 3], 14) is None                  # insufficient


def test_true_range_and_atr():
    highs = [10, 11, 12, 11]
    lows = [9, 10, 11, 10]
    closes = [9.5, 10.5, 11.5, 10.5]
    trs = F.true_ranges(highs, lows, closes)
    assert len(trs) == 3
    assert F.atr(highs, lows, closes, 2) is not None
    assert F.atr(highs, lows, closes, 10) is None


def test_bollinger_constant_series_zero():
    assert F.bollinger_bandwidth([5.0] * 20, 20) == 0.0   # zero stdev
    assert F.bollinger_bandwidth([1, 2, 3], 20) is None


def test_donchian_and_stdev():
    highs = [10, 12, 11, 13, 12] * 4
    lows = [8, 9, 8, 10, 9] * 4
    w = F.donchian_width_pct(highs, lows, 20)
    assert w is not None and w > 0
    assert F.stdev([2, 2, 2]) == 0.0
    assert F.stdev([1, 3]) == 1.0


def test_trend_slope_sign():
    up = F.trend_slope(list(range(1, 30)), 20)
    down = F.trend_slope(list(range(30, 1, -1)), 20)
    assert up is not None and up > 0
    assert down is not None and down < 0


def test_build_feature_snapshot_missing_safe():
    # Too few bars: indicators are None, not zero.
    snap = F.build_feature_snapshot("s1", "SOLUSD", "5m", [1.0, 1.1], [0.9, 1.0], [1.0, 1.05])
    assert snap.last_price == 1.05
    assert snap.ema200 is None
    assert snap.rsi14 is None
    # Enough bars: core indicators populate.
    closes = [100 + i * 0.1 for i in range(60)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    snap2 = F.build_feature_snapshot("s2", "SOLUSD", "5m", highs, lows, closes)
    assert snap2.ema20 is not None and snap2.ema50 is not None
    assert snap2.rsi14 is not None
    assert snap2.trend_slope is not None and snap2.trend_slope > 0
