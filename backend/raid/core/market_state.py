"""RAID Stage-C market-state spine (SHADOW — measure-only).

A layered, independently-logged market-state system that runs BESIDE the legacy 5m regime classifier
(raid.core.regime) WITHOUT replacing it: it books nothing, feeds no decision, changes no sizing/exit.
Every layer computes from COMPLETED bars only (never the forming candle) so there is no look-ahead.

Thresholds are SEEDED from the master prompt's starting values (INITIAL only). They are NOT proven:
the raw metric values are logged/persisted so calibrated thresholds can be derived from the live
distributions as the window fills (§8/§21). Do not treat the seeded values as final.

Layers (each independently returned + logged):
  F1 PORTFOLIO_RISK_STATE  RISK_ON / RISK_OFF / MIXED / CRISIS / UNKNOWN   (majors + breadth)
  F2 FAST_DIRECTION        LONG / SHORT / NEUTRAL / UNKNOWN  (>=3 aligned votes, no opposing veto)
  F3 EXCURSION_VETO        bool  (a recent sharp counter-excursion vetoes the fast direction)
  F4 MARKET_STRUCTURE      TREND_UP / TREND_DOWN / RANGE / UNKNOWN  (confirmed HH-HL / LH-LL)
  F5 CROSS_SECTIONAL       breadth %-up, median return, dispersion across the alt universe
UNKNOWN fails closed everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from raid.core import features as F

# ── SEEDED thresholds (INITIAL only — calibrate from live distributions; NOT final) ──────────────
SEED = {
    "slope_min": 0.0008,        # |normalized slope| for a directional vote (regime TREND_SLOPE_MIN)
    "crisis_atr_1h_pct": 0.030,  # 1h ATR% for CRISIS (regime CRISIS_ATR_PCT)
    "risk_on_breadth": 0.60,    # fraction of alts up for RISK_ON
    "risk_off_breadth": 0.40,   # fraction of alts up at/below which RISK_OFF
    "excursion_atr_mult": 1.5,  # counter-wick >= this * 5m ATR vetoes the fast direction
    "min_votes": 3,             # F2 aligned-vote threshold
    "min_bars_5m": 25,          # completed 5m bars required
    "min_bars_1h": 15,          # completed 1h bars required for ATR(14)
    "struct_lookback": 5,       # swing lookback for HH-HL / LH-LL
}


class PortfolioState(str, Enum):
    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"
    MIXED = "MIXED"
    CRISIS = "CRISIS"
    UNKNOWN = "UNKNOWN"


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class Structure(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    UNKNOWN = "UNKNOWN"


def completed(candles, now_epoch=None, interval_s=300):
    """No-look-ahead guard: return only COMPLETED bars. The scanner keeps the in-progress bar as
    candles[-1]; the spine must NEVER read it. If now_epoch is given and the last bar's open is in
    the current (unfinished) interval, drop it; if it already completed, keep it. Default (no
    now_epoch): drop the last bar (conservative)."""
    if not candles:
        return []
    if now_epoch is not None:
        try:
            last_ts = int(float(candles[-1][0]))
            window_start = int(now_epoch // interval_s) * interval_s
            return list(candles) if last_ts < window_start else list(candles[:-1])
        except (TypeError, ValueError, IndexError):
            return list(candles[:-1])
    return list(candles[:-1])


def _ohlc(candles):
    highs = [float(c[2]) for c in candles if len(c) >= 5]
    lows = [float(c[3]) for c in candles if len(c) >= 5]
    closes = [float(c[4]) for c in candles if len(c) >= 5]
    return highs, lows, closes


def vwap(candles):
    """VWAP over the given (completed) candles: sum(typical_price * volume) / sum(volume)."""
    num = den = 0.0
    for c in candles:
        if len(c) >= 6:
            tp = (float(c[2]) + float(c[3]) + float(c[4])) / 3.0
            v = float(c[5])
            num += tp * v
            den += v
    return (num / den) if den > 0 else None


def _structure(highs, lows, lookback=None):
    """Confirmed HH-HL (up) / LH-LL (down) / else RANGE from the last two swing windows."""
    lb = lookback or SEED["struct_lookback"]
    if len(highs) < lb * 2 or len(lows) < lb * 2:
        return Structure.UNKNOWN
    recent_h, prev_h = max(highs[-lb:]), max(highs[-lb * 2:-lb])
    recent_l, prev_l = min(lows[-lb:]), min(lows[-lb * 2:-lb])
    if recent_h > prev_h and recent_l > prev_l:
        return Structure.TREND_UP
    if recent_h < prev_h and recent_l < prev_l:
        return Structure.TREND_DOWN
    return Structure.RANGE


def f4_market_structure(bars_completed):
    highs, lows, closes = _ohlc(bars_completed)
    if len(closes) < SEED["min_bars_5m"]:
        return Structure.UNKNOWN
    return _structure(highs, lows)


def f2_fast_direction(bars_completed):
    """>=3 aligned completed-bar votes — close vs VWAP, EMA9 vs EMA21, normalized slope, structure.
    Returns (Direction, votes). NEUTRAL if <3 aligned either way; UNKNOWN on insufficient data."""
    highs, lows, closes = _ohlc(bars_completed)
    if len(closes) < SEED["min_bars_5m"]:
        return Direction.UNKNOWN, {}
    vw = vwap(bars_completed)
    ema9, ema21 = F.ema(closes, 9), F.ema(closes, 21)
    slope = F.trend_slope(closes, 20)
    struct = _structure(highs, lows)
    last = closes[-1]
    up = down = 0
    votes: dict = {}
    if vw is not None:
        votes["vwap"] = "up" if last > vw else "down"
        up += int(last > vw)
        down += int(last < vw)
    if ema9 is not None and ema21 is not None:
        votes["ema"] = "up" if ema9 > ema21 else "down"
        up += int(ema9 > ema21)
        down += int(ema9 < ema21)
    if slope is not None:
        if slope >= SEED["slope_min"]:
            votes["slope"] = "up"; up += 1
        elif slope <= -SEED["slope_min"]:
            votes["slope"] = "down"; down += 1
        else:
            votes["slope"] = "flat"
    if struct == Structure.TREND_UP:
        votes["structure"] = "up"; up += 1
    elif struct == Structure.TREND_DOWN:
        votes["structure"] = "down"; down += 1
    else:
        votes["structure"] = "flat"
    if up >= SEED["min_votes"] and up > down:
        return Direction.LONG, votes
    if down >= SEED["min_votes"] and down > up:
        return Direction.SHORT, votes
    return Direction.NEUTRAL, votes


def f3_excursion_veto(bars_completed, direction):
    """A recent sharp counter-excursion (wick against `direction` >= excursion_atr_mult * 5m ATR%)
    vetoes the fast direction. Returns True if vetoed. Non-directional inputs never veto."""
    highs, lows, closes = _ohlc(bars_completed)
    if len(closes) < SEED["min_bars_5m"] or direction not in (Direction.LONG, Direction.SHORT):
        return False
    atrp = F.atr_pct(highs, lows, closes, 14)
    if not atrp:
        return False
    thresh = atrp * SEED["excursion_atr_mult"]
    # Counter-wick of EACH recent bar relative to ITS OWN close (an intrabar reversal), not the
    # final close — otherwise a trending series reads its own advance as a counter-excursion.
    for cl, h, l in zip(closes[-3:], highs[-3:], lows[-3:]):
        if cl <= 0:
            continue
        wick = (cl - l) / cl if direction == Direction.LONG else (h - cl) / cl
        if wick >= thresh:
            return True
    return False


def f5_cross_sectional(alt_returns):
    """Breadth across the alt universe: fraction up, median return, dispersion (population stdev)."""
    rs = [float(r) for r in (alt_returns or []) if r is not None]
    if not rs:
        return {"n": 0, "pct_up": None, "median_return": None, "dispersion": None}
    rs_sorted = sorted(rs)
    n = len(rs)
    mean = sum(rs) / n
    return {
        "n": n,
        "pct_up": sum(1 for r in rs if r > 0) / n,
        "median_return": rs_sorted[n // 2],
        "dispersion": (sum((r - mean) ** 2 for r in rs) / n) ** 0.5,
    }


def f1_portfolio_risk_state(majors, breadth):
    """majors: list of {symbol, atr_1h_pct, dir} for BTC/ETH/SOL (whichever collected). breadth:
    f5_cross_sectional result. CRISIS if any major's 1h ATR% >= crisis threshold; else
    RISK_ON/RISK_OFF/MIXED from major-trend agreement + breadth. UNKNOWN (fail closed) if no
    majors or no breadth."""
    majors = [m for m in (majors or []) if m]
    if not majors or breadth is None or breadth.get("pct_up") is None:
        return PortfolioState.UNKNOWN
    if any((m.get("atr_1h_pct") or 0) >= SEED["crisis_atr_1h_pct"] for m in majors):
        return PortfolioState.CRISIS
    up = sum(1 for m in majors if m.get("dir") == "up")
    down = sum(1 for m in majors if m.get("dir") == "down")
    b = breadth["pct_up"]
    if up > down and b >= SEED["risk_on_breadth"]:
        return PortfolioState.RISK_ON
    if down > up and b <= SEED["risk_off_breadth"]:
        return PortfolioState.RISK_OFF
    return PortfolioState.MIXED


@dataclass
class MarketState:
    portfolio: PortfolioState
    fast_direction: Direction
    excursion_veto: bool
    structure: Structure
    breadth: dict
    majors: list
    reference_symbol: str | None = None
    votes: dict = field(default_factory=dict)


def compute_market_state(majors, breadth, reference_bars_completed, reference_symbol=None):
    """Combine the five layers into a MarketState. `reference_bars_completed` drives F2/F3/F4 (the
    market leader, e.g. BTC — already completed-bar filtered by the caller); majors + breadth drive
    F1/F5. Pure; no I/O; UNKNOWN fails closed. The excursion veto NEUTRALISES the fast direction
    (logged, not acted on)."""
    struct = f4_market_structure(reference_bars_completed)
    fdir, votes = f2_fast_direction(reference_bars_completed)
    veto = f3_excursion_veto(reference_bars_completed, fdir)
    if veto and fdir in (Direction.LONG, Direction.SHORT):
        fdir = Direction.NEUTRAL
    portfolio = f1_portfolio_risk_state(majors, breadth)
    return MarketState(portfolio=portfolio, fast_direction=fdir, excursion_veto=veto,
                       structure=struct, breadth=breadth, majors=majors,
                       reference_symbol=reference_symbol, votes=votes)
