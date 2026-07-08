"""C7 short sleeve (paper) — flag-gated enable, mirroring C3's audited short path.

Proves: short entry opens in TREND_DOWN when the flag is ON; SL ABOVE / TP BELOW entry; a
zero-volume bar blocks the short (shared build_candidate gate); flag OFF => no C7 short (shadow-
logged instead); C7-long unaffected in TREND_UP and guarded OFF in TREND_DOWN; and the shared
short PnL sign is net of the 1.04% cost. Plain asserts (run_all)."""

from decimal import Decimal

import config
from raid.core.candidate import Direction, MarketRegime
from raid.core.features import FeatureSnapshot
from raid.core.provider import CAP_SHORT, CAP_SPOT_LONG
from raid.core.strategy import StrategyContext
from raid.strategies.rotation import C7CrossSectionalMomentum


def _feat(timeframe="5m", **kw) -> FeatureSnapshot:
    base = dict(snapshot_id="ft", symbol="SOLUSD", timeframe=timeframe, last_price=100.0,
                ema20=99.0, ema50=98.0, ema200=95.0, rsi14=50.0, atr_pct=0.01,
                bb_bandwidth=0.05, donchian_pct=0.05, realized_vol=0.4,
                swing_high=101.0, swing_low=95.0, trend_slope=-0.001)
    base.update(kw)
    return FeatureSnapshot(**base)


def _candles(latest_vol=100.0, prior_vol=100.0, n=25):
    bars = [[i * 300, 100.0, 100.5, 99.5, 100.0, prior_vol] for i in range(n - 1)]
    bars.append([(n - 1) * 300, 100.0, 100.5, 99.5, 100.0, latest_vol])
    return bars


def _rank(rank, n, ret):
    return {"rank": rank, "n": n, "score": ret, "return_24h": ret, "risk_adj_momentum": ret,
            "realized_vol": 0.1, "vol_trend": 1.2, "trend_quality": 0.8}


def _ctx(regime=MarketRegime.TREND_DOWN, caps=frozenset({CAP_SPOT_LONG, CAP_SHORT}),
         rank=10, n=10, ret=-0.05, candles=None, ref=100.0, spine_dir=None) -> StrategyContext:
    extras = {"equity": 10000.0, "risk_pct": 0.005, "expiry_ts": "2026-07-02T00:20:00Z",
              "universe_rankings": {"SOLUSD": _rank(rank, n, ret)},
              "candles_5m": _candles() if candles is None else candles}
    if spine_dir is not None:
        extras["spine_dir"] = spine_dir       # Stage-D: C7 long branch gates on the reconciled spine
    return StrategyContext(
        symbol="SOLUSD", instrument_id="SOLUSD", timestamp="2026-07-02T00:00:00Z",
        market_regime=regime, features={"5m": _feat("5m"), "1h": _feat("1h")},
        market_data_snapshot_id="md", reference_price=Decimal(str(ref)),
        spread_pct=0.0004, depth_ok=True, capabilities=caps, extras=extras)


def _c7():
    return C7CrossSectionalMomentum()


# (a) short entry opens a paper position in TREND_DOWN when enabled
def test_c7_short_opens_in_trend_down_when_enabled():
    config.C7_SHORT_ENABLED = True
    cands = _c7().generate_candidates(_ctx())
    assert len(cands) == 1
    assert cands[0].direction == Direction.SHORT and cands[0].strategy_id == "RAID-C7"


# (b)+(c) short SL ABOVE entry, TP BELOW entry
def test_c7_short_sl_above_tp_below():
    config.C7_SHORT_ENABLED = True
    c = _c7().generate_candidates(_ctx())[0]
    e = float(c.reference_price)
    assert float(c.stop_price) > e        # short SL above entry (loss on a RISE)
    assert float(c.targets[0]) < e        # short TP below entry (profit on a FALL)


# (e) zero-volume bar BLOCKS a C7 short entry (shared build_candidate gate)
def test_c7_short_blocked_by_zero_volume():
    config.C7_SHORT_ENABLED = True
    assert _c7().generate_candidates(_ctx(candles=_candles(latest_vol=0.0))) == []


# (f) flag OFF => no C7 short (shadow-logged, not booked)
def test_c7_short_flag_off_no_short():
    try:
        config.C7_SHORT_ENABLED = False
        ctx = _ctx()
        assert _c7().generate_candidates(ctx) == []
        assert ctx.extras.get("_c7_shadow_shorts"), "expected the laggard to be shadow-logged"
    finally:
        config.C7_SHORT_ENABLED = True


# (g) regression: C7-long fires on a LONG spine; gated OFF when the spine resolves the pair SHORT
def test_c7_long_unaffected_in_trend_up():
    config.C7_SHORT_ENABLED = True
    cands = _c7().generate_candidates(_ctx(regime=MarketRegime.TREND_UP, rank=1, n=10, ret=0.05, spine_dir="LONG"))
    assert len(cands) == 1 and cands[0].direction == Direction.LONG


def test_c7_no_long_in_trend_down():
    # Stage-D: a down tape resolves the pair SHORT -> the spine gate blocks the long even at rank 1.
    config.C7_SHORT_ENABLED = True
    assert _c7().generate_candidates(
        _ctx(regime=MarketRegime.TREND_DOWN, rank=1, n=10, ret=0.05, spine_dir="SHORT")) == []


# (d) shared short PnL sign is net of the 1.04% round-trip cost
def test_short_pnl_sign_net_of_cost():
    from executor import compute_pnl
    assert abs(compute_pnl("short", 100.0, 95.0, 1000.0) - 39.6) < 1e-6    # fell -> +50 gross -10.4 fee
    assert abs(compute_pnl("short", 100.0, 105.0, 1000.0) - (-60.4)) < 1e-6  # rose -> -50 gross -10.4 fee


# (h) SHIPPED default is shadow (False) — robust to in-suite mutation via a fresh reload.
def test_c7_short_ships_disabled_by_default():
    import importlib
    importlib.reload(config)                    # re-execute config.py from source (env unchanged)
    assert config.C7_SHORT_ENABLED is False     # the shipped default is shadow, not live-booking
