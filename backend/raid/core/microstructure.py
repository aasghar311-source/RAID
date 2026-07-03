"""Order-book / price-action microstructure (Section 8, C10 data contract).

detect_liquidity_sweep() validates a liquidity sweep — an abnormal displacement wick
that grabs liquidity beyond a recent swing level and then REJECTS, confirmed by a
volume spike and an imbalanced order book. Pure function of data the scanner already
collects (5m candles + top-of-book walls); no new data source, no LLM.

A sweep is only reported when ALL five conditions hold, so the C10 strategy trades a
validated reversal rather than every long wick.
"""

from __future__ import annotations

# Kraken candle row: [ts, open, high, low, close, volume].
_O, _H, _L, _C, _V = 1, 2, 3, 4, 5

# Detection thresholds (documented so they are tunable, not buried magic numbers).
_MIN_BARS = 21              # need >=20 prior bars for the volume average + swing window
_WICK_BODY_MULT = 2.0      # rejection wick must exceed 2x the candle body
_VOLUME_MULT = 2.0         # sweep bar volume must exceed 2x the trailing average
_BREACH_PCT = 0.003        # wick must penetrate the swing level by >= 0.3%
_BOOK_RATIO = 1.5          # post-sweep book imbalance (support side / opposite side)
_SWING_LOOKBACK = 20


def _row(c):
    return float(c[_O]), float(c[_H]), float(c[_L]), float(c[_C]), (float(c[_V]) if len(c) > _V else 0.0)


def _depth(order_book, key: str) -> float:
    """Sum the USD depth of a book side ('bid_walls' / 'ask_walls') from the scanner's
    top-of-book wall structure. Returns 0.0 if the side is missing/empty."""
    total = 0.0
    for w in (order_book or {}).get(key, []) or []:
        try:
            total += float(w.get("usd", 0.0))
        except (AttributeError, TypeError, ValueError):
            continue
    return total


def detect_liquidity_sweep(candles_5m, order_book) -> dict | None:
    """Return a validated sweep dict, or None.

    Bullish (long) sweep — ALL must hold:
      1. DISPLACEMENT  lower wick > 2x body on the latest 5m bar
      2. VOLUME SPIKE  latest volume > 2x the trailing 20-bar average
      3. REJECTION     close back above the open (closed green)
      4. LEVEL BREACH  wick low penetrated the prior swing low by >= 0.3%
      5. BOOK SUPPORT  bid-side depth > 1.5x ask-side depth
    Bearish (short) sweep is the mirror image (reported for shadow logging only).

    Returns {direction, sweep_level, wick_low|wick_high, rejection_price,
    wick_depth_pct, volume_ratio, book_ratio} or None.
    """
    if not candles_5m or len(candles_5m) < _MIN_BARS:
        return None
    try:
        o, h, l, c, v = _row(candles_5m[-1])
    except (IndexError, TypeError, ValueError):
        return None
    if c <= 0 or l <= 0 or h <= 0:
        return None

    prior = candles_5m[:-1]
    try:
        prior_vols = [float(x[_V]) if len(x) > _V else 0.0 for x in prior[-_SWING_LOOKBACK:]]
        prior_lows = [float(x[_L]) for x in prior[-_SWING_LOOKBACK:]]
        prior_highs = [float(x[_H]) for x in prior[-_SWING_LOOKBACK:]]
    except (IndexError, TypeError, ValueError):
        return None
    if not prior_vols or not prior_lows or not prior_highs:
        return None

    avg_vol = sum(prior_vols) / len(prior_vols)
    volume_ratio = (v / avg_vol) if avg_vol > 0 else 0.0
    body = abs(c - o)
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    swing_low = min(prior_lows)
    swing_high = max(prior_highs)
    bid_depth = _depth(order_book, "bid_walls")
    ask_depth = _depth(order_book, "ask_walls")

    displaced = _WICK_BODY_MULT * body  # wick must clear this (body may be ~0 on a doji)

    # --- Bullish sweep -----------------------------------------------------
    if (
        lower_wick > 0 and lower_wick > displaced
        and volume_ratio > _VOLUME_MULT
        and c >= o
        and l < swing_low * (1 - _BREACH_PCT)
        and ask_depth > 0 and bid_depth > _BOOK_RATIO * ask_depth
    ):
        return {
            "direction": "long",
            "sweep_level": swing_low,
            "wick_low": l,
            "rejection_price": c,
            "wick_depth_pct": (c - l) / c,
            "volume_ratio": volume_ratio,
            "book_ratio": (bid_depth / ask_depth) if ask_depth > 0 else 0.0,
        }

    # --- Bearish sweep (shadow-only; short capability disabled) ------------
    if (
        upper_wick > 0 and upper_wick > displaced
        and volume_ratio > _VOLUME_MULT
        and c <= o
        and h > swing_high * (1 + _BREACH_PCT)
        and bid_depth > 0 and ask_depth > _BOOK_RATIO * bid_depth
    ):
        return {
            "direction": "short",
            "sweep_level": swing_high,
            "wick_high": h,
            "rejection_price": c,
            "wick_depth_pct": (h - c) / c,
            "volume_ratio": volume_ratio,
            "book_ratio": (ask_depth / bid_depth) if bid_depth > 0 else 0.0,
        }

    return None
