"""Appendix-C §2 pair liquidity / volume / cost metrics — MEASURE-ONLY (C.6).

Pure functions computing the 15 §2 metrics per pair from completed 5m candles + the order book +
the 24h ticker volume, in USD-quote terms. NO LOOK-AHEAD: the forming (uncommitted) 5m bar is
dropped (drop_forming) before every volume metric — this is the B.4 completed-candle fix folded in
per §2. Unavailable metrics return None (fail-closed; the C.7 tier classifier treats None as
DISABLED). No DB, no config, no network — feeds NO decision yet (C.8 does the gating).

The 15 metrics (confirmed against §2): VOLUME(7) dollar_vol_24h, dollar_vol_30d_median,
dollar_vol_5m_median, latest_5m_vol_usd, volume_ratio, zero_volume_rate, low_volume_rate;
LIQUIDITY(5) spread_pct, depth_10bps_usd, depth_25bps_usd, slippage_p50, slippage_p90;
COST(3) dynamic_cost_pct, target_cost_multiple, net_rr.

Definitions here are this repo's reconstruction of §2 (names confirmed by the operator); the
order book is TOP-3 walls only, so depth/slippage are conservative best-effort lower bounds.
"""

from statistics import median

import costs

BPS = 1e-4

# Reference sizes/geometry for the size-dependent metrics (calibration inputs, NOT thresholds):
REF_ORDER_USD_P50 = 500.0     # a typical paper position notional — slippage_p50 fills this
REF_ORDER_USD_P90 = 2000.0    # a large order — slippage_p90 fills this
VOL_RATIO_WINDOW = 20         # trailing bars for volume_ratio
REF_RR = 2.0                  # reference reward:risk for the pair-level net_rr characterisation
# §2 low_volume_rate = fraction of recent completed 5m bars whose USD volume is below this ABSOLUTE
# floor (chronic thinness — parallel to zero_volume_rate). Set to the OPPORTUNISTIC latest-5m floor
# ($250): reads ~0 for genuinely liquid pairs, high only for truly thin ones. (An earlier relative
# "< 0.35x trailing mean" definition measured burstiness and wrongly disabled liquid pairs.)
LOW_VOLUME_USD_FLOOR = 250.0
LOW_VOLUME_WINDOW = 60        # recent completed 5m bars (~5h) for the low-volume rate


# ---- completed-candle guard (B.4 folded in) ----
def drop_forming(candles, now_epoch, interval_s: int = 300):
    """Return `candles` with the LAST bar removed IF it is still forming (opened within the current,
    unfinished interval). No now_epoch -> return as-is. Pure; never raises."""
    if not candles:
        return []
    if now_epoch is None:
        return list(candles)
    try:
        last_ts = int(float(candles[-1][0]))
        window_start = int(now_epoch // interval_s) * interval_s
        return list(candles[:-1]) if last_ts >= window_start else list(candles)
    except Exception:  # noqa: BLE001
        return list(candles)


def _base_vol(bar):
    try:
        return float(bar[5])
    except Exception:  # noqa: BLE001
        return None


def _usd_vol(bar):
    """USD-quote volume of a bar = base_volume * close."""
    try:
        return float(bar[5]) * float(bar[4])
    except Exception:  # noqa: BLE001
        return None


# ---- VOLUME (7) ----
def dollar_vol_5m_median(completed_5m):
    xs = [v for v in (_usd_vol(b) for b in completed_5m) if v is not None]
    return median(xs) if xs else None


def dollar_vol_30d_median(completed_1d, days: int = 30):
    """Median USD-quote daily volume over the last `days` COMPLETED daily bars. None if no daily
    history (fail-closed -> the tier classifier treats a pair with None here as failing the active-
    tier 30d minimum)."""
    xs = [v for v in (_usd_vol(b) for b in (completed_1d or [])[-days:]) if v is not None]
    return median(xs) if xs else None


def trailing20_vol_usd(completed_5m, window: int = 20):
    """Mean USD-quote volume over the last `window` COMPLETED 5m bars (§5-9 trailing20). None if
    fewer than `window` bars (fail-closed)."""
    xs = [v for v in (_usd_vol(b) for b in completed_5m[-window:]) if v is not None]
    return (sum(xs) / len(xs)) if len(xs) >= window else None


def latest_5m_vol_usd(completed_5m):
    return _usd_vol(completed_5m[-1]) if completed_5m else None


def volume_ratio(completed_5m, window: int = VOL_RATIO_WINDOW):
    """Latest completed 5m base-volume / trailing `window` average (excludes the latest bar). None
    when there is too little history or a zero trailing average (fail-closed)."""
    vols = [v for v in (_base_vol(b) for b in completed_5m) if v is not None]
    if len(vols) < window + 1:
        return None
    trail = vols[-(window + 1):-1]
    avg = sum(trail) / len(trail)
    return (vols[-1] / avg) if avg > 0 else None


def zero_volume_rate(completed_5m, window: int = 60):
    """Fraction of the recent `window` completed 5m bars with zero traded volume."""
    vols = [v for v in (_base_vol(b) for b in completed_5m) if v is not None][-window:]
    return (sum(1 for v in vols if v <= 0.0) / len(vols)) if vols else None


def low_volume_rate(completed_5m, usd_floor: float = LOW_VOLUME_USD_FLOOR, window: int = LOW_VOLUME_WINDOW):
    """§2: fraction of the recent `window` completed 5m bars whose USD-quote volume is below
    `usd_floor` (ABSOLUTE chronic thinness). None if no bars. Reads ~0 for liquid pairs."""
    xs = [v for v in (_usd_vol(b) for b in completed_5m[-window:]) if v is not None]
    return (sum(1 for v in xs if v < usd_floor) / len(xs)) if xs else None


# ---- LIQUIDITY (5) ----
def _levels(order_book, side):
    """Executable levels for a side ('bid'/'ask'): prefer the FULL '<side>_levels' (all fetched book
    levels within the sampled window), fall back to '<side>_walls' (top-3) for backward compat."""
    return order_book.get(side + "_levels") or order_book.get(side + "_walls") or []


def _best_bid_ask(order_book):
    bids = _levels(order_book, "bid")
    asks = _levels(order_book, "ask")
    if not bids or not asks:
        return None, None
    try:
        return max(b["price"] for b in bids), min(a["price"] for a in asks)
    except Exception:  # noqa: BLE001
        return None, None


def spread_pct(order_book):
    """(best_ask - best_bid) / mid. None if the book is empty or crossed (fail-closed)."""
    bb, ba = _best_bid_ask(order_book)
    if bb is None or ba is None:
        return None
    mid = (bb + ba) / 2
    if mid <= 0 or ba <= bb:
        return None
    return (ba - bb) / mid


def depth_within_bps(order_book, bps):
    """USD depth on BOTH sides within +/- bps of mid, summed over the FULL executable book (all
    sampled levels, not just the top-3 walls). None if the book is empty/crossed."""
    bb, ba = _best_bid_ask(order_book)
    if bb is None or ba is None or ba <= bb:
        return None
    mid = (bb + ba) / 2
    if mid <= 0:
        return None
    lo, hi = mid * (1 - bps * BPS), mid * (1 + bps * BPS)
    bids = _levels(order_book, "bid")
    asks = _levels(order_book, "ask")
    usd = sum(b["usd"] for b in bids if b.get("price", 0) >= lo)
    usd += sum(a["usd"] for a in asks if a.get("price", 0) <= hi)
    return usd


def slippage_estimate(order_book, size_usd):
    """Fraction VWAP slippage vs mid to BUY `size_usd` by walking the ask book (all sampled levels).
    None if the visible book can't cover the order (insufficient depth -> fail-closed)."""
    bb, ba = _best_bid_ask(order_book)
    if bb is None or ba is None or ba <= bb:
        return None
    mid = (bb + ba) / 2
    if mid <= 0:
        return None
    asks = sorted(_levels(order_book, "ask"), key=lambda w: w.get("price", 0))
    remaining, base_filled, quote_spent = size_usd, 0.0, 0.0
    for a in asks:
        price = a.get("price", 0)
        avail = a.get("usd", 0)
        if price <= 0 or avail <= 0:
            continue
        take = min(remaining, avail)
        base_filled += take / price
        quote_spent += take
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0 or base_filled <= 0:
        return None  # walls can't cover the order
    vwap = quote_spent / base_filled
    return max(vwap / mid - 1.0, 0.0)


def atr_pct_1h(candles_1h, price, period: int = 14):
    """Average true range over the last `period` 1h bars as a fraction of `price`. None on
    insufficient history or bad price. Used as the reference move for the cost metrics."""
    if not candles_1h or len(candles_1h) < period + 1 or not price or price <= 0:
        return None
    trs = []
    try:
        for i in range(1, len(candles_1h)):
            h, lo, pc = float(candles_1h[i][2]), float(candles_1h[i][3]), float(candles_1h[i - 1][4])
            trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    except Exception:  # noqa: BLE001
        return None
    if len(trs) < period:
        return None
    return (sum(trs[-period:]) / period) / price


# ---- COST (3) ----
def cost_metrics(spread, atr_pct, rr_ref: float = REF_RR):
    """dynamic_cost_pct (real-spread round-trip, buffer excluded — same basis as the A.1 gate),
    target_cost_multiple (how many round-trip costs a 1-ATR move covers), and a reference net_rr for
    a standard rr_ref:1 ATR-stop setup net of that cost. Returns (cost, multiple, net_rr); any of the
    latter two is None when atr_pct/cost is missing/non-positive."""
    if spread is None or spread < 0:
        return None, None, None
    cost = costs.dynamic_round_trip_cost_pct(spread_pct=spread, uncertainty_buffer_pct=0.0)["total_pct"]
    if not atr_pct or atr_pct <= 0 or cost <= 0:
        return cost, None, None
    multiple = atr_pct / cost
    gross_reward, gross_risk = rr_ref * atr_pct, atr_pct
    net_reward, net_risk = gross_reward - cost, gross_risk + cost
    net_rr = (net_reward / net_risk) if net_risk > 0 else None
    return cost, multiple, net_rr


# ---- aggregate ----
def compute_pair_liquidity(symbol, ohlcv_5m, order_book, price, now_epoch, atr_pct=None,
                           volume_24h_usd=None, ohlcv_1d=None):
    """All 15 §2 metrics for one pair, plus the forming-bar volume_ratio (for the B.4 before/after)
    and the completed-bar count. Volume metrics use ONLY completed bars (5m and daily). Pure; missing
    inputs -> None on the affected metrics (never raises)."""
    completed = drop_forming(ohlcv_5m, now_epoch)
    completed_1d = drop_forming(ohlcv_1d, now_epoch, interval_s=86400)   # drop today's forming day
    forming_vr = volume_ratio(ohlcv_5m)          # what A.2 gates on TODAY (forming bar included)
    spread = spread_pct(order_book)
    cost, tcm, nrr = cost_metrics(spread, atr_pct)
    return {
        "symbol": symbol,
        # VOLUME(7)
        "dollar_vol_24h": float(volume_24h_usd) if volume_24h_usd is not None else None,
        "dollar_vol_30d_median": dollar_vol_30d_median(completed_1d),
        "dollar_vol_5m_median": dollar_vol_5m_median(completed),
        "trailing20_vol_usd": trailing20_vol_usd(completed),
        "latest_5m_vol_usd": latest_5m_vol_usd(completed),
        "volume_ratio": volume_ratio(completed),  # §2: COMPLETED-candle
        "zero_volume_rate": zero_volume_rate(completed),
        "low_volume_rate": low_volume_rate(completed),
        # LIQUIDITY(5)
        "spread_pct": spread,
        "depth_10bps_usd": depth_within_bps(order_book, 10),
        "depth_25bps_usd": depth_within_bps(order_book, 25),
        "slippage_p50": slippage_estimate(order_book, REF_ORDER_USD_P50),
        "slippage_p90": slippage_estimate(order_book, REF_ORDER_USD_P90),
        # COST(3)
        "dynamic_cost_pct": cost,
        "target_cost_multiple": tcm,
        "net_rr": nrr,
        # B.4 completed-vs-forming instrumentation
        "volume_ratio_forming": forming_vr,
        "completed_bars": len(completed),
    }
