"""Cross-sectional universe ranking (Section 8, C6/C7 data contract).

Pure functions over the scanner's existing OHLCV — NO new data source. Each cycle the
runner calls compute_universe_rankings() ONCE over all scan results and threads the
result into every per-symbol StrategyContext via extras, so the relative-strength
rotation (C6) and cross-sectional momentum (C7) strategies can see the whole universe
from a single-symbol context.

Ranking horizon is the hourly series (ohlcv_1h): 24 hourly bars = a clean 24h window,
robust regardless of the 5m candle count. Every metric is a reproducible function of
the input bars — no LLM, no randomness, no network.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

# Kraken candle row shape used across the engine: [ts, open, high, low, close, volume].
_CLOSE_IDX = 4
_VOL_IDX = 5

# Ranking horizon (hourly bars). A symbol needs the FULL horizon of history before it
# is ranked, so every symbol is compared over the same true 24h window (a freshly-listed
# pair with <24h of hourly bars is simply left unranked, not ranked on a short horizon).
_HORIZON_BARS = 24
_MIN_BARS = _HORIZON_BARS + 1
_VOL_FLOOR = 1e-4          # floor on realized vol so a near-flat symbol can't inflate momentum
_VOL_AVG_WINDOW = 20

_TAG_RE = re.compile(r"^(RAID-C\d+)")


def parse_strategy_tag(reasoning) -> str | None:
    """Extract the 'RAID-Cn' strategy id from a trade's claude_reasoning tag, or None."""
    if not reasoning:
        return None
    m = _TAG_RE.match(str(reasoning))
    return m.group(1) if m else None


def _parse_iso(ts) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def within_cooldown(last_iso, now_iso, hours: float) -> bool:
    """True if `last_iso` is within `hours` before `now_iso` (i.e. still cooling down).

    A missing/unparseable last timestamp means NOT in cooldown (free to act). Used by
    C6's rebalance limiter to avoid fee-destroying churn every 20-minute cycle.
    """
    last = _parse_iso(last_iso)
    now = _parse_iso(now_iso) or datetime.now(timezone.utc)
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() < hours * 3600.0


def parse_margin(reasoning) -> float | None:
    """Extract the tagged margin ('margin=200.00') from a trade's claude_reasoning, or None."""
    m = re.search(r"margin=([\d.]+)", str(reasoning or ""))
    return float(m.group(1)) if m else None


def trade_margin(trade: dict) -> float:
    """Margin used by an open trade for the deployment cap. Leveraged trades tag 'margin=X';
    pre-leverage trades carry no leverage so their notional (size_usd) IS the margin."""
    m = parse_margin(trade.get("claude_reasoning"))
    if m is not None:
        return m
    return float(trade.get("size_usd") or 0)


def has_opposite(open_directions, direction: str) -> bool:
    """True if an OPPOSITE-direction position is already open on the symbol (so a new
    entry would hedge/net-out on one symbol — block it). Same-direction stacking returns
    False (allowed). Pure + testable."""
    opposite = "short" if direction in ("long", "yes") else "long"
    return opposite in (open_directions or set())


def _closes_vols(candles) -> tuple[list[float], list[float]]:
    closes, vols = [], []
    for c in candles or []:
        try:
            closes.append(float(c[_CLOSE_IDX]))
            vols.append(float(c[_VOL_IDX]) if len(c) > _VOL_IDX else 0.0)
        except (IndexError, TypeError, ValueError):
            continue
    return closes, vols


def _stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = sum(values) / n
    return math.sqrt(sum((v - m) ** 2 for v in values) / n)


def _r_squared(ys: list[float]) -> float:
    """R² of a least-squares fit of ys against time — a 0..1 'trend smoothness' score
    (direction-agnostic). 1.0 = a perfectly straight line, 0.0 = no linear structure."""
    n = len(ys)
    if n < 3:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    if sxx <= 0 or syy <= 0:
        return 0.0
    r = sxy / math.sqrt(sxx * syy)
    return max(0.0, min(1.0, r * r))


def _metrics(candles) -> dict | None:
    """Per-symbol ranking metrics from an hourly OHLCV series, or None if too short."""
    closes, vols = _closes_vols(candles)
    if len(closes) < _MIN_BARS or closes[-1] <= 0:
        return None

    horizon = min(_HORIZON_BARS, len(closes) - 1)
    base = closes[-1 - horizon]
    if base <= 0:
        return None
    return_24h = closes[-1] / base - 1.0

    # Realized volatility of hourly log returns over the horizon window.
    window = closes[-(horizon + 1):]
    rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window)) if window[i - 1] > 0]
    realized_vol = _stdev(rets)
    risk_adj_momentum = return_24h / max(realized_vol, _VOL_FLOOR)

    # Volume trend: latest bar volume vs its trailing average.
    recent_vols = vols[-_VOL_AVG_WINDOW:] if vols else []
    avg_vol = (sum(recent_vols) / len(recent_vols)) if recent_vols else 0.0
    vol_trend = (vols[-1] / avg_vol) if avg_vol > 0 else 1.0

    trend_quality = _r_squared(window)

    return {
        "return_24h": return_24h,
        "realized_vol": realized_vol,
        "risk_adj_momentum": risk_adj_momentum,
        "vol_trend": vol_trend,
        "trend_quality": trend_quality,
        # Primary ranking key: risk-adjusted momentum. Kept separate from the raw
        # components so strategies can apply their own secondary gates.
        "score": risk_adj_momentum,
    }


def compute_universe_rankings(scan_results) -> dict:
    """Rank every symbol with usable hourly data by risk-adjusted momentum.

    Returns {symbol: {rank, n, score, return_24h, risk_adj_momentum, realized_vol,
    vol_trend, trend_quality}} where rank 1 = strongest. Symbols without enough hourly
    history are omitted (no rank) rather than ranked on fabricated data.
    """
    metrics: dict[str, dict] = {}
    for sr in scan_results or []:
        symbol = getattr(sr, "symbol", None) or (sr.get("symbol") if isinstance(sr, dict) else None)
        candles = getattr(sr, "ohlcv_1h", None)
        if candles is None and isinstance(sr, dict):
            candles = sr.get("ohlcv_1h")
        if not symbol:
            continue
        m = _metrics(candles)
        if m is not None:
            metrics[symbol] = m

    ranked = sorted(metrics.items(), key=lambda kv: kv[1]["score"], reverse=True)
    n = len(ranked)
    out: dict[str, dict] = {}
    for i, (symbol, m) in enumerate(ranked):
        out[symbol] = {"rank": i + 1, "n": n, **m}
    return out
