"""§16 A+ component scoring (proposed design). Validates the band->leverage ladder and that the
composite behaves: a strong setup reaches A+/A++, and a thin-edge (low net_rr) setup is gated down."""

from decimal import Decimal
from types import SimpleNamespace

from raid.core.candidate import Direction, MarketRegime
from raid.core.features import FeatureSnapshot
from raid.core.provider import CAP_SPOT_LONG
from raid.core.strategy import StrategyContext
from raid.core import scoring as S


def _feat(tf, ema20, ema50, ema200):
    return FeatureSnapshot(snapshot_id="ft", symbol="X", timeframe=tf, last_price=100.0, ema20=ema20,
                           ema50=ema50, ema200=ema200, rsi14=50.0, atr_pct=0.01, bb_bandwidth=0.05,
                           donchian_pct=0.05, realized_vol=0.4, swing_high=101.0, swing_low=99.0,
                           trend_slope=0.001)


def _ctx(spine_dir, pf, vrc, spread, feats, rankings=None):
    ex = {"spine_dir": spine_dir, "spine_portfolio": pf, "vol_ratio_completed": vrc}
    if rankings is not None:
        ex["universe_rankings"] = rankings
    return StrategyContext(symbol="X", instrument_id="X", timestamp="t",
                           market_regime=MarketRegime.TREND_UP, features=feats,
                           market_data_snapshot_id="md", reference_price=Decimal("100"),
                           spread_pct=spread, depth_ok=True,
                           capabilities=frozenset({CAP_SPOT_LONG}), extras=ex)


def _cand(direction, net_rr):
    return SimpleNamespace(direction=direction, net_rr=Decimal(str(net_rr)))


def test_band_ladder():
    assert S.quality_leverage(79.9) == 0.0          # reject
    assert S.quality_leverage(80.0) == 1.5          # ordinary floor
    assert S.quality_leverage(87.9) == 1.5
    assert S.quality_leverage(88.0) == 2.25         # A+
    assert S.quality_leverage(93.9) == 2.25
    assert S.quality_leverage(94.0) == 3.0          # A++
    assert (S.band_label(60), S.band_label(84), S.band_label(90), S.band_label(96)) == \
        ("REJECT", "ordinary", "A+", "A++")


def _strong_long_ctx():
    up = {tf: _feat(tf, 101.0, 100.0, 99.0) for tf in ("5m", "15m", "30m", "1h")}  # up-stacked
    return _ctx("LONG", "RISK_ON", vrc=1.6, spread=0.0004, feats=up, rankings={"X": {"rank": 1, "n": 20}})


def test_strong_setup_reaches_aplus_or_better():
    r = S.score_candidate(_strong_long_ctx(), _cand(Direction.LONG, 2.6))
    assert r.score >= 88 and r.quality_lev >= 2.25, r.log_str()


def test_thin_edge_low_netrr_gated_down():
    # identical strong context, but net_rr barely above the 1.20 cost floor -> post_cost_econ ~0
    r = S.score_candidate(_strong_long_ctx(), _cand(Direction.LONG, 1.25))
    assert r.score < 88, r.log_str()                # cannot reach A+ on a thin edge alone
    assert r.components["post_cost_econ"][0] < 10   # the net_rr drag is the cause


def test_weak_context_rejects():
    # spine mismatch + no HTF alignment + thin edge -> REJECT
    flat = {tf: _feat(tf, 100.0, 100.0, 100.0) for tf in ("5m", "15m", "30m", "1h")}
    ctx = _ctx("NEUTRAL", "MIXED", vrc=0.5, spread=0.0025, feats=flat)
    r = S.score_candidate(ctx, _cand(Direction.LONG, 1.25))
    assert r.quality_lev == 0.0, r.log_str()        # below the ordinary floor
