"""Fail-closed hard-zero volume gate (shared, helpers.build_candidate) + _volume_confirmed fix.

Proves: (a) a zero-volume latest 5m bar => NO candidate, at build_candidate and via strategy
paths; (b) missing/insufficient/None volume => rejected; (c) a genuine SMALL positive ratio (0.1)
still passes the shared gate (no thin filter was added); (d) _volume_confirmed fails CLOSED on
missing/zero while keeping its 1.5x threshold for positive-volume bars. Plain asserts (run_all)."""

from decimal import Decimal

import config
from raid.core.candidate import Direction, EntryType, MarketRegime
from raid.core.features import FeatureSnapshot
from raid.core.provider import CAP_SPOT_LONG
from raid.core.strategy import StrategyContext
from raid.strategies.catalog import build_default_registry
from raid.strategies.helpers import build_candidate
from raid.strategies.trend import _volume_confirmed


def _candles(latest_vol=100.0, prior_vol=100.0, n=25):
    """n bars [ts,o,h,l,c,v]; prior bars = prior_vol, latest bar = latest_vol. n>=21 so
    volume_ratio (needs 21) computes. ratio = latest_vol/prior_vol."""
    bars = [[i * 300, 100.0, 100.5, 99.5, 100.0, prior_vol] for i in range(n - 1)]
    bars.append([(n - 1) * 300, 100.0, 100.5, 99.5, 100.0, latest_vol])
    return bars


def _feat(**kw) -> FeatureSnapshot:
    base = dict(snapshot_id="ft", symbol="SOLUSD", timeframe="5m", last_price=100.0,
                ema20=99.0, ema50=98.0, ema200=95.0, rsi14=50.0, atr_pct=0.008,
                bb_bandwidth=0.05, donchian_pct=0.05, realized_vol=0.4,
                swing_high=100.0, swing_low=95.0, trend_slope=0.001)
    base.update(kw)
    return FeatureSnapshot(**base)


_SENT = object()


def _ctx(candles=_SENT, regime=MarketRegime.TREND_UP, feat=None, caps=frozenset({CAP_SPOT_LONG})):
    extras = {"equity": 10000.0, "risk_pct": 0.005, "expiry_ts": "2026-07-02T00:20:00Z",
              "candles_5m": (_candles() if candles is _SENT else candles)}
    return StrategyContext(
        symbol="SOLUSD", instrument_id="SOLUSD", timestamp="2026-07-02T00:00:00Z",
        market_regime=regime, features={"5m": feat or _feat()},
        market_data_snapshot_id="md", reference_price=Decimal("100"),
        spread_pct=0.0004, depth_ok=True, capabilities=caps, extras=extras)


def _build(ctx):
    """Call the shared chokepoint directly with an economic long (1% risk / 4.4% reward)."""
    return build_candidate(
        strategy_id="RAID-CT", strategy_version="t", code_version="t", ctx=ctx,
        direction=Direction.LONG, entry_type=EntryType.MARKET, timeframe="5m",
        reference_price=100.0, stop_price=99.0, targets=(104.4,),
        expiry_ts="2026-07-02T00:20:00Z", capability_requirements=(CAP_SPOT_LONG,))


# (a) hard-zero latest bar => rejected
def test_gate_rejects_hard_zero_direct():
    assert _build(_ctx(candles=_candles(latest_vol=0.0))) is None


def test_gate_rejects_hard_zero_via_strategy_paths():
    reg = build_default_registry()
    c1, c4 = reg.get("RAID-C1"), reg.get("RAID-C4")
    f1 = _feat(last_price=99.5, swing_high=100.0, ema20=99.0, ema50=98.0, atr_pct=0.008)
    assert c1.generate_candidates(_ctx(candles=_candles(latest_vol=0.0), feat=f1)) == []
    f4 = _feat(last_price=95.4, swing_low=95.0, swing_high=104.0, rsi14=38.0)   # C4 has NO own vol gate
    assert c4.generate_candidates(_ctx(candles=_candles(latest_vol=0.0), regime=MarketRegime.RANGE, feat=f4)) == []


# (b) missing / insufficient / None => rejected
def test_gate_rejects_missing_and_insufficient():
    assert _build(_ctx(candles=None)) is None            # candles_5m missing
    assert _build(_ctx(candles=[])) is None              # empty
    assert _build(_ctx(candles=_candles(n=10))) is None  # <21 bars -> volume_ratio None


# (c) A.2 universal thin-volume floor (0.35): ratio below the floor is REJECTED; at/above passes
def test_thin_volume_floor():
    assert config.MIN_VOLUME_RATIO == 0.35                                                  # A.2 flip live
    assert _build(_ctx(candles=_candles(latest_vol=10.0, prior_vol=100.0))) is None         # ratio 0.10 < 0.35
    assert _build(_ctx(candles=_candles(latest_vol=30.0, prior_vol=100.0))) is None         # ratio 0.30 < 0.35
    assert _build(_ctx(candles=_candles(latest_vol=50.0, prior_vol=100.0))) is not None     # ratio 0.50 >= 0.35
    reg = build_default_registry()
    c4 = reg.get("RAID-C4")   # no 1.5x threshold of its own -> exercises ONLY the shared gate
    f4 = _feat(last_price=95.4, swing_low=95.0, swing_high=104.0, rsi14=38.0)
    assert c4.generate_candidates(_ctx(candles=_candles(latest_vol=10.0, prior_vol=100.0),
                                       regime=MarketRegime.RANGE, feat=f4)) == []            # 0.10 rejected
    cands = c4.generate_candidates(_ctx(candles=_candles(latest_vol=50.0, prior_vol=100.0),
                                        regime=MarketRegime.RANGE, feat=f4))
    assert len(cands) == 1                                                                   # 0.50 passes


# regression: normal positive volume still emits (long/positive path unchanged)
def test_positive_volume_unchanged():
    assert _build(_ctx()) is not None
    reg = build_default_registry()
    c1 = reg.get("RAID-C1")
    f1 = _feat(last_price=99.5, swing_high=100.0, ema20=99.0, ema50=98.0, atr_pct=0.008)
    cands = c1.generate_candidates(_ctx(candles=_candles(latest_vol=200.0, prior_vol=100.0), feat=f1))
    assert len(cands) == 1   # ratio 2.0 clears C1's 1.5x AND the shared gate


# (d) _volume_confirmed fails CLOSED on missing/zero; 1.5x threshold unchanged for positive bars
def test_volume_confirmed_fails_closed_on_missing_zero():
    assert _volume_confirmed([]) == (False, 0.0)          # <21 bars (was fail-open True)
    assert _volume_confirmed(None) == (False, 0.0)
    allzero = [[i * 300, 1, 1, 1, 1, 0.0] for i in range(25)]
    assert _volume_confirmed(allzero) == (False, 0.0)     # zero average (was fail-open True)
    ok = [[i * 300, 1, 1, 1, 1, 100.0] for i in range(24)] + [[24 * 300, 1, 1, 1, 1, 200.0]]
    conf, ratio = _volume_confirmed(ok)                   # ratio 2.0 >= 1.5 -> confirmed (unchanged)
    assert conf is True and abs(ratio - 2.0) < 1e-9
    low = [[i * 300, 1, 1, 1, 1, 100.0] for i in range(25)]   # ratio 1.0 < 1.5
    conf2, _ = _volume_confirmed(low)
    assert conf2 is False
