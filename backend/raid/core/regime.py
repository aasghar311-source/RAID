"""Deterministic market-regime classification.

Maps a FeatureSnapshot to one of {trend_up, trend_down, range, volatile, crisis,
unknown}. Pure function of features — no LLM. Strategies gate their eligibility on
the regime, so this is a shared, testable primitive rather than per-strategy guesswork.

Thresholds are module constants (tunable, and later per-symbol calibratable). They
are intentionally conservative: when data is insufficient the result is UNKNOWN and
the no-trade path applies.
"""

from __future__ import annotations

from dataclasses import dataclass

from raid.core.candidate import MarketRegime
from raid.core.features import FeatureSnapshot

# Per-bar ATR% thresholds (fraction of price). Calibrated for 5m crypto bars.
CRISIS_ATR_PCT = 0.030      # >3.0% ATR per bar = disorderly
VOLATILE_ATR_PCT = 0.012    # >1.2% ATR per bar = elevated
# Trend-slope magnitude (normalized least-squares slope) that counts as trending.
TREND_SLOPE_MIN = 0.0008
# EMA separation (fraction) required to confirm a stacked trend.
EMA_STACK_MIN = 0.001


@dataclass(frozen=True)
class RegimeAssessment:
    regime: MarketRegime
    reasons: tuple[str, ...]


def classify(f: FeatureSnapshot) -> RegimeAssessment:
    reasons: list[str] = []

    # Need at least price + short trend + volatility to classify.
    if f.last_price <= 0 or f.trend_slope is None or f.atr_pct is None:
        return RegimeAssessment(MarketRegime.UNKNOWN, ("insufficient_data",))

    atrp = f.atr_pct
    slope = f.trend_slope

    # 1) Crisis dominates everything — disorderly volatility.
    if atrp >= CRISIS_ATR_PCT:
        reasons.append(f"atr_pct={atrp:.4f}>=crisis({CRISIS_ATR_PCT})")
        return RegimeAssessment(MarketRegime.CRISIS, tuple(reasons))

    # 2) Directional trend, only when volatility is not elevated enough to be VOLATILE.
    stacked_up = (
        f.ema20 is not None and f.ema50 is not None
        and f.ema20 > f.ema50 * (1 + EMA_STACK_MIN)
        and f.last_price >= f.ema20
    )
    stacked_down = (
        f.ema20 is not None and f.ema50 is not None
        and f.ema20 < f.ema50 * (1 - EMA_STACK_MIN)
        and f.last_price <= f.ema20
    )

    if atrp < VOLATILE_ATR_PCT:
        if slope >= TREND_SLOPE_MIN and stacked_up:
            reasons.append(f"slope={slope:.4f}>0 & ema20>ema50 & price>=ema20")
            return RegimeAssessment(MarketRegime.TREND_UP, tuple(reasons))
        if slope <= -TREND_SLOPE_MIN and stacked_down:
            reasons.append(f"slope={slope:.4f}<0 & ema20<ema50 & price<=ema20")
            return RegimeAssessment(MarketRegime.TREND_DOWN, tuple(reasons))
        reasons.append(f"low_slope={slope:.4f} & atr_pct={atrp:.4f}<volatile")
        return RegimeAssessment(MarketRegime.RANGE, tuple(reasons))

    # 3) Elevated (but not crisis) volatility with no clean stacked trend = VOLATILE.
    #    A strong stacked trend riding elevated vol still reads as a (volatile) trend.
    if slope >= TREND_SLOPE_MIN and stacked_up:
        reasons.append(f"trend_up on elevated atr_pct={atrp:.4f}")
        return RegimeAssessment(MarketRegime.TREND_UP, tuple(reasons))
    if slope <= -TREND_SLOPE_MIN and stacked_down:
        reasons.append(f"trend_down on elevated atr_pct={atrp:.4f}")
        return RegimeAssessment(MarketRegime.TREND_DOWN, tuple(reasons))

    reasons.append(f"atr_pct={atrp:.4f}>=volatile({VOLATILE_ATR_PCT}), no clean trend")
    return RegimeAssessment(MarketRegime.VOLATILE, tuple(reasons))
