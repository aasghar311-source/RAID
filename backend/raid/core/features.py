"""Deterministic feature engine — pure indicator math over OHLC bars.

No LLM, no randomness, no network. Every value is a reproducible function of the
input bars, so a candidate can cite feature_snapshot_id and be re-derived exactly.
Hand-rolled (no numpy) to match the existing signals.py and keep deploy deps light.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


def sma(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period  # seed with SMA of first `period`
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))


def true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return trs


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    trs = true_ranges(highs, lows, closes)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def atr_pct(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    a = atr(highs, lows, closes, period)
    if a is None or not closes or closes[-1] <= 0:
        return None
    return a / closes[-1]


def stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = sum(values) / n
    return math.sqrt(sum((v - m) ** 2 for v in values) / n)


def bollinger_bandwidth(closes: list[float], period: int = 20, mult: float = 2.0) -> float | None:
    """(upper - lower) / mid — a normalized volatility/compression measure."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    if mid <= 0:
        return None
    sd = stdev(window)
    return (2 * mult * sd) / mid


def donchian_width_pct(highs: list[float], lows: list[float], period: int = 20) -> float | None:
    if len(highs) < period or len(lows) < period or not highs:
        return None
    hi = max(highs[-period:])
    lo = min(lows[-period:])
    ref = highs[-1]
    if ref <= 0:
        return None
    return (hi - lo) / ref


def realized_vol_annualized(closes: list[float], bars_per_year: float = 365 * 288) -> float | None:
    """Annualized realized vol from log returns (default assumes 5m bars)."""
    if len(closes) < 3:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    return stdev(rets) * math.sqrt(bars_per_year)


def swing_high(highs: list[float], lookback: int = 20) -> float | None:
    return max(highs[-lookback:]) if len(highs) >= 1 else None


def swing_low(lows: list[float], lookback: int = 20) -> float | None:
    return min(lows[-lookback:]) if len(lows) >= 1 else None


def trend_slope(values: list[float], period: int = 20) -> float | None:
    """Least-squares slope of the last `period` values, normalized by their mean.
    Positive = up, negative = down. A cheap, deterministic trend proxy."""
    if len(values) < period or period < 2:
        return None
    ys = values[-period:]
    xs = list(range(period))
    mx = sum(xs) / period
    my = sum(ys) / period
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(period))
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0 or my == 0:
        return None
    return (num / den) / my


@dataclass(frozen=True)
class FeatureSnapshot:
    """Immutable bundle of features for one symbol/timeframe at one instant."""

    snapshot_id: str
    symbol: str
    timeframe: str
    last_price: float
    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    rsi14: float | None = None
    atr_pct: float | None = None
    bb_bandwidth: float | None = None
    donchian_pct: float | None = None
    realized_vol: float | None = None
    swing_high: float | None = None
    swing_low: float | None = None
    trend_slope: float | None = None
    extras: dict = field(default_factory=dict)


def build_feature_snapshot(
    snapshot_id: str,
    symbol: str,
    timeframe: str,
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> FeatureSnapshot:
    """Compute the standard feature set from OHLC. Missing-data-safe: any indicator
    without enough bars is None (a strategy must treat None as no-trade, not zero)."""
    last = closes[-1] if closes else 0.0
    return FeatureSnapshot(
        snapshot_id=snapshot_id,
        symbol=symbol,
        timeframe=timeframe,
        last_price=last,
        ema20=ema(closes, 20),
        ema50=ema(closes, 50),
        ema200=ema(closes, 200),
        rsi14=rsi(closes, 14),
        atr_pct=atr_pct(highs, lows, closes, 14),
        bb_bandwidth=bollinger_bandwidth(closes, 20),
        donchian_pct=donchian_width_pct(highs, lows, 20),
        realized_vol=realized_vol_annualized(closes),
        swing_high=swing_high(highs, 20),
        swing_low=swing_low(lows, 20),
        trend_slope=trend_slope(closes, 20),
    )
