"""Tests for the strategy catalog and the functional trend/range/vol strategies."""

from decimal import Decimal

from raid.core.candidate import Candidate, Direction, MarketRegime
from raid.core.features import FeatureSnapshot
from raid.core.provider import CAP_SPOT_LONG, CAP_SHORT
from raid.core.strategy import Strategy, StrategyContext
from raid.strategies.catalog import ALL_STRATEGY_IDS, build_default_registry


def _feat(**kw) -> FeatureSnapshot:
    base = dict(
        snapshot_id="ft", symbol="SOLUSD", timeframe="5m", last_price=100.0,
        ema20=99.0, ema50=98.0, ema200=95.0, rsi14=50.0, atr_pct=0.008,
        bb_bandwidth=0.05, donchian_pct=0.05, realized_vol=0.4,
        swing_high=100.0, swing_low=95.0, trend_slope=0.001,
    )
    base.update(kw)
    return FeatureSnapshot(**base)


def _pos_vol_candles(n=25, prior_vol=100.0, latest_vol=200.0):
    """n 5m bars [ts,o,h,l,c,v]; latest bar 2x the prior average -> volume_ratio 2.0. Positive
    (satisfies the fail-closed hard-zero gate in build_candidate) AND >= C1's 1.5x breakout
    confirmation, so the existing emit tests exercise the real positive-volume path (production
    always has real volume; the old fixtures relied on _volume_confirmed's now-removed fail-open)."""
    bars = [[i * 300, 100.0, 100.5, 99.5, 100.0, prior_vol] for i in range(n - 1)]
    bars.append([(n - 1) * 300, 100.0, 100.5, 99.5, 100.0, latest_vol])
    return bars


def _ctx(regime: MarketRegime, feat: FeatureSnapshot, caps=frozenset({CAP_SPOT_LONG}), spread=0.0004) -> StrategyContext:
    return StrategyContext(
        symbol="SOLUSD", instrument_id="SOLUSD", timestamp="2026-07-02T00:00:00Z",
        market_regime=regime, features={"5m": feat},
        market_data_snapshot_id="md", reference_price=Decimal("100"),
        spread_pct=spread, depth_ok=True, capabilities=caps,
        extras={"equity": 10000.0, "risk_pct": 0.005, "expiry_ts": "2026-07-02T00:20:00Z",
                "candles_5m": _pos_vol_candles()},
    )


def test_all_ten_registered_and_conform():
    reg = build_default_registry()
    assert len(reg) == 10
    assert sorted(reg.ids()) == sorted(ALL_STRATEGY_IDS)
    for s in reg.all():
        assert isinstance(s, Strategy)
        assert s.strategy_id.startswith("RAID-C")
        # interface present
        assert callable(s.generate_candidates)
        assert callable(s.should_exit)
        assert callable(s.is_eligible)


def test_c1_breakout_emits_valid_candidate():
    reg = build_default_registry()
    c1 = reg.get("RAID-C1")
    # Price just below resistance(100), stacked uptrend -> breakout setup.
    feat = _feat(last_price=99.5, swing_high=100.0, ema20=99.0, ema50=98.0, atr_pct=0.008)
    ctx = _ctx(MarketRegime.TREND_UP, feat)
    assert c1.is_eligible(ctx) is True
    cands = c1.generate_candidates(ctx)
    assert len(cands) == 1
    c = cands[0]
    assert isinstance(c, Candidate)
    assert c.direction == Direction.LONG
    assert c.strategy_id == "RAID-C1"
    assert c.net_rr >= Decimal("1.25")           # cleared the cost hurdle
    assert c.quantity > 0 and c.planned_risk_dollars > 0


def test_c1_no_trade_when_extended_or_wrong_regime():
    reg = build_default_registry()
    c1 = reg.get("RAID-C1")
    # Wrong regime -> not eligible.
    ctx_range = _ctx(MarketRegime.RANGE, _feat(last_price=99.5))
    assert c1.is_eligible(ctx_range) is False
    # Eligible regime but price extended far above resistance -> no candidate.
    feat_ext = _feat(last_price=110.0, swing_high=100.0, ema20=99.0, ema50=98.0)
    assert c1.generate_candidates(_ctx(MarketRegime.TREND_UP, feat_ext)) == []


def test_c4_range_reversion_emits_candidate():
    reg = build_default_registry()
    c4 = reg.get("RAID-C4")
    # Wide range 95..104, price near low, oversold RSI.
    feat = _feat(last_price=95.4, swing_low=95.0, swing_high=104.0, rsi14=38.0)
    ctx = _ctx(MarketRegime.RANGE, feat)
    assert c4.is_eligible(ctx) is True
    cands = c4.generate_candidates(ctx)
    assert len(cands) == 1
    assert cands[0].direction == Direction.LONG
    assert cands[0].net_rr >= Decimal("1.20")


def test_c3_short_is_shadow_gated():
    reg = build_default_registry()
    c3 = reg.get("RAID-C3")
    feat = _feat(last_price=95.2, swing_low=95.0, ema20=98.0, ema50=99.0)
    # Spot-long-only caps -> not eligible (needs short capability).
    assert c3.is_eligible(_ctx(MarketRegime.TREND_DOWN, feat)) is False
    # With short capability it becomes eligible.
    assert c3.is_eligible(_ctx(MarketRegime.TREND_DOWN, feat, caps=frozenset({CAP_SHORT}))) is True


def test_shadow_strategies_decline_cleanly():
    # C6/C7/C10 were activated to paper; C8 (pairs/short) and C9 (futures/margin) remain
    # shadow until their capability contract is met.
    reg = build_default_registry()
    for sid in ("RAID-C8", "RAID-C9"):
        s = reg.get(sid)
        ctx = _ctx(MarketRegime.TREND_UP, _feat())
        assert s.generate_candidates(ctx) == []
        # records why it declined
        assert sid in ctx.extras.get("_shadow_declined", {})


def test_exit_on_adverse_regime():
    reg = build_default_registry()
    c1 = reg.get("RAID-C1")
    pos = {"direction": "long"}
    hold = c1.should_exit(pos, _ctx(MarketRegime.TREND_UP, _feat()))
    assert hold is None
    ex = c1.should_exit(pos, _ctx(MarketRegime.CRISIS, _feat()))
    assert ex is not None and ex.should_exit is True
