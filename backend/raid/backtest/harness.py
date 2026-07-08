"""Backtest harness — CALIBRATION PROXY, NOT EDGE.

A 5m-ONLY proxy (the live 1s exit path is NOT stored, per the standing rule). VALID ONLY for: firing
rate, candidate count, which pairs, spine direction at fire, trigger-metric distributions, threshold
recalibration, and relative / directional comparison. It is NOT proof of profitability or absolute
edge — that still requires live paper. Every reported figure is a "calibration proxy, not edge".

Reconstruction reuses the SAME live functions (raid.core market_state / features / liquidity) so the
per-pair spine direction each strategy sees here matches what it will see live. NO LOOK-AHEAD: bar i
is the latest COMPLETED bar and sees ONLY bars[0..i] — never the forming bar or any future H/L/C.
"""

from __future__ import annotations

from decimal import Decimal

from raid.core import features as F, liquidity as L, market_state as MS
from raid.core.regime import classify
from raid.core.strategy import StrategyContext

BREADTH_LOOKBACK_BARS = 288      # 24h of 5m bars — a bar is backtestable once it has this history
_NOMINAL_SPREAD = 0.001          # OHLCV store has no book; nominal 0.1% spread (liquid-pair proxy)


def _ohlc(bars):
    return ([float(b[2]) for b in bars], [float(b[3]) for b in bars], [float(b[4]) for b in bars])


def major_at(bars_upto_i, symbol):
    """{symbol, atr_1h_pct, dir} for a major from its 5m bars up to i (structure = up/down/flat; ATR%
    as a 5m-based proxy for the 1h ATR the live CRISIS check uses — CRISIS is rare, proxy acceptable)."""
    h, l, c = _ohlc(bars_upto_i)
    atrp = F.atr_pct(h, l, c, 14) if len(c) >= 15 else None
    st = MS._structure(h, l) if len(c) >= 10 else MS.Structure.UNKNOWN
    d = "up" if st == MS.Structure.TREND_UP else "down" if st == MS.Structure.TREND_DOWN else "flat"
    return {"symbol": symbol, "atr_1h_pct": atrp, "dir": d}


def breadth_at(closes_by_sym, i, lookback=BREADTH_LOOKBACK_BARS):
    """F5 breadth at bar index i from each pair's 24h (lookback-bar) return. Only pairs with >= i+1
    bars and a close `lookback` bars back contribute (no look-ahead — uses close[i] vs close[i-lb])."""
    rets = []
    for cl in closes_by_sym.values():
        if len(cl) > i and i - lookback >= 0 and cl[i - lookback]:
            rets.append((cl[i] - cl[i - lookback]) / cl[i - lookback])
    return MS.f5_cross_sectional(rets)


def portfolio_at(majors_bars_by_sym, closes_by_sym, i):
    """F1 portfolio state at bar i (majors' structure/ATR up to i + breadth at i). UNKNOWN fails
    closed. Pure — reuses the live MS.f1_portfolio_risk_state."""
    majors = [major_at(bars[: i + 1], sym) for sym, bars in majors_bars_by_sym.items() if len(bars) > i]
    breadth = breadth_at(closes_by_sym, i)
    return MS.f1_portfolio_risk_state(majors, breadth), breadth


def context_at(symbol, bars_upto_i, spine_dir, spine_portfolio=None):
    """Build a StrategyContext for the COMPLETED bar i (bars_upto_i = bars[0..i]). 5m features only
    (strategies fall back to the 5m ATR for the stop); nominal spread; spine_dir + spine_portfolio +
    completed-bar volume_ratio threaded into extras exactly as the live runner does."""
    h, l, c = _ohlc(bars_upto_i)
    feat = F.build_feature_snapshot(f"bt-{symbol}", symbol, "5m", h, l, c)
    px = Decimal(str(c[-1]))
    extras = {"equity": 4000.0, "risk_pct": 0.005, "expiry_ts": str(int(bars_upto_i[-1][0])),
              "candles_5m": bars_upto_i, "spine_dir": spine_dir, "spine_portfolio": spine_portfolio,
              "order_book": {}, "vol_ratio_completed": L.volume_ratio(bars_upto_i)}
    return StrategyContext(
        symbol=symbol, instrument_id=symbol, timestamp=str(int(bars_upto_i[-1][0])),
        market_regime=classify(feat).regime, features={"5m": feat},
        market_data_snapshot_id=f"bt-{symbol}", reference_price=px, spread_pct=_NOMINAL_SPREAD,
        depth_ok=True, capabilities=frozenset({"spot_long", "short", "margin"}), extras=extras)


def run(strategies, universe_bars, majors_bars, min_bar=BREADTH_LOOKBACK_BARS):
    """Run each strategy over the universe (dict {sym: bars_5m}) bar-by-bar with the reconstructed
    per-pair spine. Returns a dict per strategy_id: fires (list of records), setups (regime tally),
    trigger distributions. CALIBRATION PROXY, NOT EDGE.

    Each record: {ts, symbol, direction, spine_dir, portfolio, net_rr, vol_ratio_completed}."""
    closes_by_sym = {s: [float(b[4]) for b in bars] for s, bars in universe_bars.items()}
    n = max((len(b) for b in universe_bars.values()), default=0)
    out = {s.strategy_id: {"fires": [], "regime_bars": {}, "vr_at_setup": [], "eligible_bars": 0}
           for s in strategies}
    regime_tally = {}
    for i in range(min_bar, n):
        portfolio, _breadth = portfolio_at(majors_bars, closes_by_sym, i)
        regime_tally[portfolio.value] = regime_tally.get(portfolio.value, 0) + 1
        for sym, bars in universe_bars.items():
            if len(bars) <= i:
                continue
            window = bars[: i + 1]
            sdir, _raw, _ = MS.resolve_pair_direction(portfolio, window)
            ctx = context_at(sym, window, sdir.value, portfolio.value)
            for strat in strategies:
                rec = out[strat.strategy_id]
                rec["regime_bars"][portfolio.value] = rec["regime_bars"].get(portfolio.value, 0) + 1
                if not strat.is_eligible(ctx):
                    continue
                rec["eligible_bars"] += 1
                # trigger-metric capture for recalibration: (spine_dir, completed-bar volume_ratio)
                # at every eligible bar, so a threshold can be set from the distribution (as C3 was).
                rec["vr_at_setup"].append((sdir.value, ctx.extras.get("vol_ratio_completed")))
                cands = strat.generate_candidates(ctx)
                for cn in cands:
                    rec["fires"].append({
                        "ts": int(window[-1][0]), "symbol": sym, "direction": cn.direction.value,
                        "spine_dir": sdir.value, "portfolio": portfolio.value,
                        "net_rr": float(cn.net_rr), "vr": ctx.extras.get("vol_ratio_completed")})
    return out, regime_tally
