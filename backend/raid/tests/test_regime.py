"""Tests for deterministic regime classification."""

from raid.core.candidate import MarketRegime
from raid.core.features import FeatureSnapshot
from raid.core.regime import classify


def _snap(**kw) -> FeatureSnapshot:
    base = dict(
        snapshot_id="s", symbol="SOLUSD", timeframe="5m", last_price=100.0,
        ema20=100.0, ema50=100.0, ema200=100.0, rsi14=50.0,
        atr_pct=0.005, bb_bandwidth=0.01, donchian_pct=0.02,
        realized_vol=0.5, swing_high=105.0, swing_low=95.0, trend_slope=0.0,
    )
    base.update(kw)
    return FeatureSnapshot(**base)


def test_unknown_on_missing_data():
    assert classify(_snap(trend_slope=None)).regime == MarketRegime.UNKNOWN
    assert classify(_snap(atr_pct=None)).regime == MarketRegime.UNKNOWN
    assert classify(_snap(last_price=0.0)).regime == MarketRegime.UNKNOWN


def test_crisis():
    assert classify(_snap(atr_pct=0.05)).regime == MarketRegime.CRISIS


def test_trend_up():
    r = classify(_snap(atr_pct=0.005, trend_slope=0.002, ema20=101.0, ema50=100.0, last_price=102.0))
    assert r.regime == MarketRegime.TREND_UP


def test_trend_down():
    r = classify(_snap(atr_pct=0.005, trend_slope=-0.002, ema20=99.0, ema50=100.0, last_price=98.0))
    assert r.regime == MarketRegime.TREND_DOWN


def test_range_low_slope():
    r = classify(_snap(atr_pct=0.005, trend_slope=0.0001, ema20=100.0, ema50=100.0, last_price=100.0))
    assert r.regime == MarketRegime.RANGE


def test_volatile_without_clean_trend():
    # Elevated (not crisis) vol, no stacked trend.
    r = classify(_snap(atr_pct=0.02, trend_slope=0.0, ema20=100.0, ema50=100.0, last_price=100.0))
    assert r.regime == MarketRegime.VOLATILE


def test_volatile_trend_still_trend():
    # Strong stacked uptrend riding elevated vol reads as TREND_UP.
    r = classify(_snap(atr_pct=0.02, trend_slope=0.003, ema20=102.0, ema50=100.0, last_price=103.0))
    assert r.regime == MarketRegime.TREND_UP
