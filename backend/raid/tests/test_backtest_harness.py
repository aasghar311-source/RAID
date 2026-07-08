"""Backtest harness (CALIBRATION PROXY, NOT EDGE) — the three correctness invariants the operator
required: NO look-ahead (bar i unaffected by future bars), COMPLETED-bar only, and spine
reconstruction that reuses the live market_state functions. Pure; no DB. Run_all-discovered."""

from raid.backtest import harness as H
from raid.core import market_state as MS


def _bars(closes, vol=1000.0, wick=0.002, ts0=0, step=300):
    return [[ts0 + i * step, c, c * (1 + wick), c * (1 - wick), c, vol] for i, c in enumerate(closes)]


def test_no_lookahead_portfolio():
    down = _bars([200 - i * 0.1 for i in range(320)])
    majors = {"ETHUSD": down, "SOLUSD": down}
    closes = {"A": [b[4] for b in down], "B": [b[4] for b in down]}
    i = 300
    p1, _ = H.portfolio_at(majors, closes, i)
    # append FUTURE bars (index > i) — the result AT i must not change (no look-ahead)
    fut = _bars([100.0] * 30, ts0=down[-1][0] + 300)
    majors2 = {s: v + fut for s, v in majors.items()}
    closes2 = {s: v + [b[4] for b in fut] for s, v in closes.items()}
    p2, _ = H.portfolio_at(majors2, closes2, i)
    assert p1 == p2


def test_breadth_uses_only_past():
    closes = {"A": [100 + i for i in range(400)], "B": [100 - i * 0.5 for i in range(400)]}
    assert H.breadth_at(closes, 300)["n"] == 2
    assert H.breadth_at(closes, 100)["n"] == 0        # < 288-bar lookback -> nothing (no look-ahead)


def test_context_completed_bar_and_spine():
    bars = _bars([100 - i * 0.2 for i in range(60)])
    ctx = H.context_at("ETHUSD", bars, "SHORT")
    assert ctx.extras["spine_dir"] == "SHORT"
    assert ctx.extras["vol_ratio_completed"] is not None
    assert ctx.feature("5m") is not None
    assert float(ctx.reference_price) == bars[-1][4]  # latest COMPLETED close, never a future bar


def test_spine_reconstruction_matches_live():
    # resolve_pair_direction is the SAME function the live runner uses — RISK_OFF never emits LONG.
    assert MS.resolve_pair_direction(MS.PortfolioState.RISK_OFF, _bars([200 - i for i in range(30)]))[0] == MS.Direction.SHORT
    assert MS.resolve_pair_direction(MS.PortfolioState.RISK_OFF, _bars([100 + i for i in range(30)]))[0] == MS.Direction.NEUTRAL


def test_run_smoke():
    # tiny end-to-end: a down universe under a reconstructed RISK_OFF book — run returns structure.
    from raid.strategies.trend import C3ShortTrendBreakdown
    down = _bars([200 - i * 0.15 for i in range(320)])
    uni = {"ETHUSD": down, "SOLUSD": down}
    out, regimes = H.run([C3ShortTrendBreakdown()], uni, uni, min_bar=300)
    assert "RAID-C3" in out and isinstance(regimes, dict)
    assert sum(regimes.values()) > 0                  # bars were classified
