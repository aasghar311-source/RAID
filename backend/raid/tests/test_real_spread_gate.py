"""A.1 — real spread into the gate. build_candidate prices on ctx.spread_pct (never the 0.0004
fallback): rejects unknown/zero/over-cap spreads (fail-closed) and its net_rr reflects the DYNAMIC
round-trip cost built on the real spread. Reuses the volume-gate harness. Plain asserts (run_all)."""

from decimal import Decimal

import config
import costs
from raid.core.candidate import Direction, EntryType, MarketRegime
from raid.core.features import FeatureSnapshot
from raid.core.provider import CAP_SPOT_LONG
from raid.core.strategy import StrategyContext
from raid.strategies.helpers import build_candidate


def _candles(n=25):
    return [[i * 300, 100.0, 100.5, 99.5, 100.0, 100.0] for i in range(n)]


def _feat() -> FeatureSnapshot:
    return FeatureSnapshot(
        snapshot_id="ft", symbol="SOLUSD", timeframe="5m", last_price=100.0, ema20=99.0,
        ema50=98.0, ema200=95.0, rsi14=50.0, atr_pct=0.008, bb_bandwidth=0.05, donchian_pct=0.05,
        realized_vol=0.4, swing_high=100.0, swing_low=95.0, trend_slope=0.001)


def _ctx(spread_pct):
    extras = {"equity": 10000.0, "risk_pct": 0.005, "expiry_ts": "2026-07-02T00:20:00Z",
              "candles_5m": _candles()}
    return StrategyContext(
        symbol="SOLUSD", instrument_id="SOLUSD", timestamp="2026-07-02T00:00:00Z",
        market_regime=MarketRegime.TREND_UP, features={"5m": _feat()}, market_data_snapshot_id="md",
        reference_price=Decimal("100"), spread_pct=spread_pct, depth_ok=True,
        capabilities=frozenset({CAP_SPOT_LONG}), extras=extras)


def _build(ctx):
    return build_candidate(
        strategy_id="RAID-CT", strategy_version="t", code_version="t", ctx=ctx,
        direction=Direction.LONG, entry_type=EntryType.MARKET, timeframe="5m",
        reference_price=100.0, stop_price=99.0, targets=(104.4,),
        expiry_ts="2026-07-02T00:20:00Z", capability_requirements=(CAP_SPOT_LONG,))


def test_a1_flag_is_live():
    assert config.ENFORCE_REAL_SPREAD_DEPTH is True          # the flip is enforced


def test_rejects_over_cap_spread():
    assert _build(_ctx(config.MAX_SPREAD_PCT_UNIVERSAL + 1e-5)) is None   # too wide -> reject


def test_rejects_unknown_and_zero_spread():
    assert _build(_ctx(None)) is None                         # unknown book -> fail-closed
    assert _build(_ctx(0.0)) is None                          # zero is not a real quote -> reject
    assert _build(_ctx(-0.001)) is None                       # negative -> reject


def test_tight_spread_passes_and_prices_on_real_spread():
    sp = 0.0004
    c = _build(_ctx(sp))
    assert c is not None
    rt = costs.dynamic_round_trip_cost_pct(spread_pct=sp, uncertainty_buffer_pct=0.0)["total_pct"]
    exp = (0.044 - rt) / (0.01 + rt)
    assert abs(float(c.net_rr) - exp) < 5e-3                  # net_rr uses the real-spread cost
    assert abs(float(c.expected_spread) - sp) < 1e-9         # candidate records the real spread


def test_wider_legal_spread_costs_more():
    tight = _build(_ctx(0.0004))
    wide = _build(_ctx(config.MAX_SPREAD_PCT_UNIVERSAL - 1e-5))          # 0.249%, still under cap
    assert tight is not None and wide is not None
    assert float(wide.net_rr) < float(tight.net_rr)          # wider real spread -> lower net_rr


def test_real_spread_uses_top_of_book_not_biggest_walls():
    # A.1 FIX regression: the runtime spread must be true top-of-book (best_bid/best_ask over the FULL
    # levels, matching the tier classifier), NOT the gap between the two LARGEST *size-sorted* walls.
    # This book has a TIGHT top-of-book (99.99/100.01 -> ~0.02%) but its biggest walls sit DEEP
    # (99.00/101.00 -> ~2%). The old code returned ~2% and would reject the pair; the fix returns ~0.02%.
    from raid.runner import _real_spread_depth
    ob = {
        "bid_levels": [{"price": 99.99, "usd": 500.0}, {"price": 99.00, "usd": 50000.0}],
        "ask_levels": [{"price": 100.01, "usd": 500.0}, {"price": 101.00, "usd": 50000.0}],
        "bid_walls": [{"price": 99.00, "usd": 50000.0}, {"price": 99.99, "usd": 500.0}],
        "ask_walls": [{"price": 101.00, "usd": 50000.0}, {"price": 100.01, "usd": 500.0}],
    }
    sp, depth, ok = _real_spread_depth(ob)
    assert ok is True
    assert sp is not None and sp < 0.001          # ~0.02% top-of-book, NOT the ~2% biggest-wall gap
    assert depth > 0                              # executable depth still summed
    # crossed / empty book -> fail-closed (unknown spread)
    assert _real_spread_depth({}) == (None, None, False)
