"""Stage-C market-state spine (SHADOW) — per-layer vote logic + UNKNOWN fail-closed + no-look-ahead.

Pure functions tested directly (measure-only; the spine books nothing, feeds no decision). No DB.
Auto-discovered by raid.tests.run_all.
"""

from raid.core.market_state import (
    Direction, PortfolioState, Structure, completed, compute_market_state,
    f1_portfolio_risk_state, f2_fast_direction, f3_excursion_veto, f4_market_structure,
    f5_cross_sectional,
)


def _bars(closes, vol=100.0, wick=0.001):
    """[ts,o,h,l,c,vol] with a symmetric small wick around each close."""
    return [[i * 300, c, c * (1 + wick), c * (1 - wick), c, vol] for i, c in enumerate(closes)]


def test_completed_no_lookahead():
    bars = [[0, 1, 1, 1, 1, 1], [300, 2, 2, 2, 2, 1], [600, 3, 3, 3, 3, 1], [900, 4, 4, 4, 4, 1]]
    assert len(completed(bars)) == 3                    # default drops the forming last bar
    assert len(completed(bars, now_epoch=1200)) == 4    # last bar (open 900) already completed -> kept
    assert len(completed(bars, now_epoch=1000)) == 3    # last bar still in current interval -> dropped
    assert completed([]) == []


def test_f2_fast_direction_long_short_neutral_unknown():
    d, votes = f2_fast_direction(completed(_bars([100 + i for i in range(30)])))   # strictly rising
    assert d == Direction.LONG and votes["ema"] == "up"
    assert f2_fast_direction(completed(_bars([100 - i for i in range(30)])))[0] == Direction.SHORT
    assert f2_fast_direction(completed(_bars([100.0] * 30)))[0] == Direction.NEUTRAL
    assert f2_fast_direction(_bars([1, 2, 3]))[0] == Direction.UNKNOWN               # insufficient bars


def test_f4_market_structure():
    assert f4_market_structure(completed(_bars([100 + i for i in range(30)]))) == Structure.TREND_UP
    assert f4_market_structure(completed(_bars([100 - i for i in range(30)]))) == Structure.TREND_DOWN
    assert f4_market_structure(_bars([1, 2, 3])) == Structure.UNKNOWN


def test_f3_excursion_veto():
    bars = _bars([100 + i for i in range(29)])
    bars.append([29 * 300, 128, 128.2, 120.0, 128, 100])   # big down-wick (low 120 vs close 128)
    assert f3_excursion_veto(bars, Direction.LONG) is True
    assert f3_excursion_veto(_bars([100 + i for i in range(30)]), Direction.LONG) is False  # clean long
    assert f3_excursion_veto(bars, Direction.NEUTRAL) is False                               # non-directional


def test_f5_cross_sectional():
    r = f5_cross_sectional([0.02, -0.01, 0.03, None, -0.05])
    assert r["n"] == 4 and 0.0 <= r["pct_up"] <= 1.0
    assert r["median_return"] is not None and r["dispersion"] >= 0
    assert f5_cross_sectional([])["pct_up"] is None


def test_f1_portfolio_risk_state():
    hi = {"pct_up": 0.7, "median_return": 0.01, "dispersion": 0.02, "n": 40}
    lo = {"pct_up": 0.3, "median_return": -0.01, "dispersion": 0.02, "n": 40}
    up = [{"symbol": "BTCUSD", "atr_1h_pct": 0.01, "dir": "up"},
          {"symbol": "ETHUSD", "atr_1h_pct": 0.01, "dir": "up"}]
    down = [{"symbol": "BTCUSD", "atr_1h_pct": 0.01, "dir": "down"},
            {"symbol": "ETHUSD", "atr_1h_pct": 0.01, "dir": "down"}]
    assert f1_portfolio_risk_state(up, hi) == PortfolioState.RISK_ON
    assert f1_portfolio_risk_state(down, lo) == PortfolioState.RISK_OFF
    assert f1_portfolio_risk_state([{"symbol": "BTCUSD", "atr_1h_pct": 0.04, "dir": "down"}], lo) == PortfolioState.CRISIS
    assert f1_portfolio_risk_state([], hi) == PortfolioState.UNKNOWN          # no majors -> fail closed
    assert f1_portfolio_risk_state(up, {"pct_up": None}) == PortfolioState.UNKNOWN


def test_compute_market_state_unknown_fail_closed():
    ms = compute_market_state([], {"pct_up": None}, [])
    assert ms.portfolio == PortfolioState.UNKNOWN
    assert ms.fast_direction == Direction.UNKNOWN
    assert ms.structure == Structure.UNKNOWN


def test_compute_market_state_veto_neutralises_direction():
    bars = _bars([100 + i for i in range(29)])
    bars.append([29 * 300, 128, 128.2, 118.0, 128, 100])   # strong long + terminal down-wick
    majors = [{"symbol": "BTCUSD", "atr_1h_pct": 0.01, "dir": "up"}]
    breadth = {"pct_up": 0.7, "median_return": 0.01, "dispersion": 0.02, "n": 40}
    ms = compute_market_state(majors, breadth, bars, "BTCUSD")
    assert ms.excursion_veto is True and ms.fast_direction == Direction.NEUTRAL
