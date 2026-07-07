"""Tests for the C6/C7/C10 activation: universe ranking, sweep detection, the two
cross-sectional long strategies, the liquidity-sweep strategy, the C6 rebalance limiter,
and the no-stacking (C7-vs-C6 dedupe) gate."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import config
from scanner import _cache_fresh, _refresh_due
from raid.core.candidate import Direction, MarketRegime
from raid.core.features import FeatureSnapshot
from raid.core.microstructure import detect_liquidity_sweep
from raid.core.provider import CAP_SHORT, CAP_SPOT_LONG
from raid.core.strategy import StrategyContext
from raid.core.universe import compute_universe_rankings, parse_strategy_tag, within_cooldown
from raid.execution.time_stops import (
    C10_MAX_HOLD_MINUTES, c10_time_stop_due, classify_stop_reason, no_progress_exit_due,
)
from raid.strategies.rotation import C6RelativeStrengthRotation, C7CrossSectionalMomentum
from raid.strategies.sweep import C10LiquiditySweepReversal


# ── fixtures ────────────────────────────────────────────────────────────────

def _feat(tf="5m", **kw) -> FeatureSnapshot:
    base = dict(
        snapshot_id=f"ft-{tf}", symbol="SOLUSD", timeframe=tf, last_price=100.0,
        ema20=101.0, ema50=100.0, ema200=98.0, rsi14=55.0, atr_pct=0.01,
        bb_bandwidth=0.05, donchian_pct=0.05, realized_vol=0.4,
        swing_high=105.0, swing_low=97.0, trend_slope=0.002,
    )
    base.update(kw)
    return FeatureSnapshot(**base)


def _ctx(regime, extras=None, symbol="SOLUSD", ref=100.0, caps=frozenset({CAP_SPOT_LONG}),
         features=None) -> StrategyContext:
    ex = {"equity": 10000.0, "risk_pct": 0.005, "expiry_ts": "2026-07-02T00:20:00Z",
          "candles_5m": _flat_candles()}  # positive-volume bars: satisfy the hard-zero volume gate
    if extras:
        ex.update(extras)
    return StrategyContext(
        symbol=symbol, instrument_id=symbol, timestamp="2026-07-02T00:00:00+00:00",
        market_regime=regime, features=features or {"5m": _feat("5m"), "1h": _feat("1h")},
        market_data_snapshot_id="md", reference_price=Decimal(str(ref)),
        spread_pct=0.0004, depth_ok=True, capabilities=caps, extras=ex,
    )


def _rank(rank, n, ret=0.05, ram=0.5, tq=0.8) -> dict:
    return {"rank": rank, "n": n, "score": ram, "return_24h": ret, "risk_adj_momentum": ram,
            "realized_vol": 0.1, "vol_trend": 1.2, "trend_quality": tq}


def _flat_candles(n=24, price=100.0, vol=10.0, t0=1_700_000_000, step=300):
    return [[t0 + i * step, price, price, price, price, vol] for i in range(n)]


def _bullish_sweep_candles():
    c = _flat_candles(24)
    c.append([c[-1][0] + 300, 100.0, 100.3, 99.0, 100.2, 100.0])  # long lower wick, green close, breach 100→99
    return c


def _bearish_sweep_candles():
    c = _flat_candles(24)
    c.append([c[-1][0] + 300, 100.0, 101.0, 99.7, 99.8, 100.0])   # long upper wick, red close, breach 100→101
    return c


_BULL_OB = {"bid_walls": [{"price": 99.0, "usd": 5000.0}], "ask_walls": [{"price": 100.3, "usd": 1000.0}]}
_BEAR_OB = {"bid_walls": [{"price": 99.5, "usd": 1000.0}], "ask_walls": [{"price": 101.0, "usd": 5000.0}]}


def _ramp_candles(start, end, n=25, vol=10.0, t0=1_700_000_000, step=3600):
    closes = [start + (end - start) * i / (n - 1) for i in range(n)]
    return [[t0 + i * step, c, c, c, c, vol] for i, c in enumerate(closes)]


# ── universe ranking ────────────────────────────────────────────────────────

def test_compute_universe_rankings_orders_by_momentum():
    scans = [
        SimpleNamespace(symbol="UP", ohlcv_1h=_ramp_candles(100, 130)),
        SimpleNamespace(symbol="FLAT", ohlcv_1h=_ramp_candles(100, 100)),
        SimpleNamespace(symbol="DOWN", ohlcv_1h=_ramp_candles(100, 80)),
    ]
    r = compute_universe_rankings(scans)
    assert r["UP"]["rank"] == 1 and r["DOWN"]["rank"] == 3
    assert r["UP"]["n"] == 3
    assert r["UP"]["return_24h"] > 0 > r["DOWN"]["return_24h"]
    for key in ("risk_adj_momentum", "vol_trend", "trend_quality", "score"):
        assert key in r["UP"]


def test_compute_universe_rankings_skips_short_history():
    scans = [SimpleNamespace(symbol="TINY", ohlcv_1h=_ramp_candles(100, 110, n=4))]
    assert compute_universe_rankings(scans) == {}


# ── sweep detection ─────────────────────────────────────────────────────────

def test_detect_bullish_liquidity_sweep():
    s = detect_liquidity_sweep(_bullish_sweep_candles(), _BULL_OB)
    assert s is not None and s["direction"] == "long"
    assert s["wick_low"] == 99.0 and s["volume_ratio"] > 2.0


def test_detect_bearish_liquidity_sweep():
    s = detect_liquidity_sweep(_bearish_sweep_candles(), _BEAR_OB)
    assert s is not None and s["direction"] == "short"


def test_no_sweep_without_book_support():
    # Same displacement wick but a balanced book -> not a validated sweep.
    balanced = {"bid_walls": [{"price": 99.0, "usd": 1000.0}], "ask_walls": [{"price": 100.3, "usd": 1000.0}]}
    assert detect_liquidity_sweep(_bullish_sweep_candles(), balanced) is None


def test_no_sweep_on_quiet_bar():
    # A flat final bar (no wick, no volume spike) is not a sweep.
    assert detect_liquidity_sweep(_flat_candles(25), _BULL_OB) is None


# ── C6 relative-strength rotation ───────────────────────────────────────────

def test_c6_is_eligible_true():
    ctx = _ctx(MarketRegime.TREND_UP)
    assert C6RelativeStrengthRotation().is_eligible(ctx) is True


def test_c6_emits_long_for_top_ranked_uptrend():
    ctx = _ctx(MarketRegime.TREND_UP, extras={"universe_rankings": {"SOLUSD": _rank(1, 10)}})
    cands = C6RelativeStrengthRotation().generate_candidates(ctx)
    assert len(cands) == 1
    assert cands[0].direction == Direction.LONG and cands[0].strategy_id == "RAID-C6"
    assert cands[0].net_rr >= Decimal("1.20")


def test_c6_declines_when_outside_top_five():
    ctx = _ctx(MarketRegime.TREND_UP, extras={"universe_rankings": {"SOLUSD": _rank(7, 20)}})
    assert C6RelativeStrengthRotation().generate_candidates(ctx) == []


def test_c6_rebalance_limiter_blocks_within_cooldown():
    # Same top-ranked setup, but the runner has flagged the rebalance window closed.
    ctx = _ctx(MarketRegime.TREND_UP, extras={
        "universe_rankings": {"SOLUSD": _rank(1, 10)}, "c6_rebalance_ok": False,
    })
    assert C6RelativeStrengthRotation().generate_candidates(ctx) == []


def test_c6_does_not_stack_open_symbol():
    ctx = _ctx(MarketRegime.TREND_UP, extras={
        "universe_rankings": {"SOLUSD": _rank(1, 10)}, "open_symbols": {"SOLUSD"},
    })
    assert C6RelativeStrengthRotation().generate_candidates(ctx) == []


# ── C7 cross-sectional momentum ─────────────────────────────────────────────

def test_c7_is_eligible_true():
    assert C7CrossSectionalMomentum().is_eligible(_ctx(MarketRegime.RANGE)) is True


def test_c7_emits_long_for_top_quintile():
    ctx = _ctx(MarketRegime.TREND_UP, extras={"universe_rankings": {"SOLUSD": _rank(1, 10)}})
    cands = C7CrossSectionalMomentum().generate_candidates(ctx)
    assert len(cands) == 1 and cands[0].strategy_id == "RAID-C7"


def test_c7_bottom_quintile_is_shadow_short_only():
    ctx = _ctx(MarketRegime.RANGE, extras={"universe_rankings": {"SOLUSD": _rank(10, 10)}})
    assert C7CrossSectionalMomentum().generate_candidates(ctx) == []
    shorts = ctx.extras.get("_c7_shadow_shorts", [])
    assert shorts and shorts[0]["symbol"] == "SOLUSD"


def test_c7_does_not_duplicate_open_c6_position():
    # C6 already holds SOLUSD (in open_symbols) -> C7 must not also open it.
    ctx = _ctx(MarketRegime.TREND_UP, extras={
        "universe_rankings": {"SOLUSD": _rank(1, 10)}, "open_symbols": {"SOLUSD"},
    })
    assert C7CrossSectionalMomentum().generate_candidates(ctx) == []


# ── C10 liquidity-sweep reversal ────────────────────────────────────────────

def test_c10_is_eligible_true_in_volatile():
    assert C10LiquiditySweepReversal().is_eligible(_ctx(MarketRegime.VOLATILE)) is True


def test_c10_sweep_tradeable_long_only():
    assert C10LiquiditySweepReversal.sweep_tradeable("long") is True
    assert C10LiquiditySweepReversal.sweep_tradeable("short") is False


def test_c10_emits_long_on_bullish_sweep():
    ctx = _ctx(MarketRegime.VOLATILE, ref=100.2, extras={
        "candles_5m": _bullish_sweep_candles(), "order_book": _BULL_OB,
    })
    cands = C10LiquiditySweepReversal().generate_candidates(ctx)
    assert len(cands) == 1
    assert cands[0].direction == Direction.LONG and cands[0].strategy_id == "RAID-C10"


def test_c10_bearish_sweep_is_shadow_only():
    ctx = _ctx(MarketRegime.VOLATILE, ref=99.8, extras={
        "candles_5m": _bearish_sweep_candles(), "order_book": _BEAR_OB,
    })
    assert C10LiquiditySweepReversal().generate_candidates(ctx) == []
    assert ctx.extras.get("_c10_shadow")           # recorded as a shadow short
    assert ctx.extras.get("_c10_sweeps")           # but still logged as a detected sweep


def test_c10_time_stop_after_max_hold():
    strat = C10LiquiditySweepReversal()
    ctx = _ctx(MarketRegime.VOLATILE)
    old = strat.should_exit({"open_time": "2020-01-01T00:00:00+00:00"}, ctx)
    assert old is not None and old.should_exit is True and old.reason == "sweep_time_stop"
    assert strat.should_exit({"open_time": None}, ctx) is None


def test_c10_executor_time_stop_predicate():
    # This exercises the PRODUCTION exit predicate used by executor.monitor_positions
    # (strategy.should_exit is not called in production). Tag-scoping is the critical
    # property: a RAID-C1 trade of the same age must NEVER be caught by the C10 stop.
    now = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
    old = (now - timedelta(minutes=C10_MAX_HOLD_MINUTES + 1)).isoformat()
    fresh = (now - timedelta(minutes=10)).isoformat()
    assert c10_time_stop_due("RAID-C10 market net_rr=1.30 :: x", old, now) is True
    assert c10_time_stop_due("RAID-C10 market net_rr=1.30 :: x", fresh, now) is False
    # prefix safety: RAID-C1 must not be swept up by a startswith on "RAID-C10"
    assert c10_time_stop_due("RAID-C1 stop net_rr=1.30 :: x", old, now) is False
    assert c10_time_stop_due("RAID-C2 limit net_rr=1.30 :: x", old, now) is False
    # fails closed on missing data
    assert c10_time_stop_due(None, old, now) is False
    assert c10_time_stop_due("RAID-C10 ...", None, now) is False


# ── helpers: tag parsing + cooldown ─────────────────────────────────────────

def test_parse_strategy_tag():
    assert parse_strategy_tag("RAID-C6 market net_rr=1.43 :: x") == "RAID-C6"
    assert parse_strategy_tag("RAID-C10 market net_rr=1.30 :: y") == "RAID-C10"
    assert parse_strategy_tag("legacy reasoning") is None
    assert parse_strategy_tag(None) is None


def test_within_cooldown():
    assert within_cooldown("2026-07-02T00:00:00+00:00", "2026-07-02T01:00:00+00:00", 2.0) is True
    assert within_cooldown("2026-07-02T00:00:00+00:00", "2026-07-02T03:00:00+00:00", 2.0) is False
    assert within_cooldown(None, "2026-07-02T03:00:00+00:00", 2.0) is False


# ── FIX 1: C1 quarantine ─────────────────────────────────────────────────────

def test_all_ten_paper_c1_unquarantined():
    # C1 was un-quarantined (with a 1.5x volume filter). All ten strategies are now in paper
    # mode (8 produce candidates; C8/C9 are data-gated stubs). _QUARANTINED is empty.
    from raid.core.strategy import StrategyMode
    from raid.runner import build_cutover_registry, _QUARANTINED
    reg = build_cutover_registry()
    assert "RAID-C1" not in _QUARANTINED
    assert reg.mode("RAID-C1") == StrategyMode.PAPER
    paper_ids = {s.strategy_id for s in reg.paper()}
    assert paper_ids == {f"RAID-C{i}" for i in range(1, 11)}   # all 10 in paper mode


# ── FIX 2: no-progress exit (production predicate) ───────────────────────────

def test_no_progress_exit_fires_at_90min_below_threshold():
    # +0.2% at 90 min (< 0.3%) → cut.
    assert no_progress_exit_due("long", 100.0, 100.2, 90.0, 90.0, 0.003) is True
    # short side mirrors.
    assert no_progress_exit_due("short", 100.0, 99.8, 95.0, 90.0, 0.003) is True


def test_no_progress_exit_holds_when_green_enough():
    # +0.5% at 90 min (>= 0.3%) → keep.
    assert no_progress_exit_due("long", 100.0, 100.5, 90.0, 90.0, 0.003) is False


def test_no_progress_exit_does_not_fire_before_check_minutes():
    # At 60 min it is too early regardless of gain.
    assert no_progress_exit_due("long", 100.0, 100.0, 60.0, 90.0, 0.003) is False


# ── FIX 4: trail labeling ────────────────────────────────────────────────────

def test_trail_labeling_long():
    assert classify_stop_reason("long", 100.0, 101.0) == "trailing_stop"   # SL above entry = trailed
    assert classify_stop_reason("long", 100.0, 99.0) == "stop_loss"        # SL below entry = true stop


def test_trail_labeling_short_and_baddata():
    assert classify_stop_reason("short", 100.0, 99.0) == "trailing_stop"   # SL below entry = trailed (short)
    assert classify_stop_reason("short", 100.0, 101.0) == "stop_loss"
    assert classify_stop_reason("long", 0, 99.0) == "stop_loss"            # fails closed on bad entry


# ── FIX 3: C4 RSI ceiling loosened 45 -> 50 ──────────────────────────────────

def _c4_feat(rsi):
    # Wide range (95..104) so the reversion-to-mid target clears the HONEST 1.04% gate; this
    # test isolates the RSI ceiling (48 admits, 55 rejects), not the economics. A narrow range
    # would now be honestly gated out on net_rr, masking the RSI behavior under test.
    return _feat("5m", last_price=95.3, swing_low=95.0, swing_high=104.0, rsi14=rsi, atr_pct=0.008)


def test_c4_rsi_ceiling_admits_neutral_rsi():
    from raid.strategies.meanrev import C4RangeMeanReversion
    c4 = C4RangeMeanReversion()
    # RSI 48 is above the OLD 45 ceiling but within the new 50 ceiling → now fires.
    cands = c4.generate_candidates(_ctx(MarketRegime.RANGE, features={"5m": _c4_feat(48.0)}))
    assert len(cands) == 1 and cands[0].strategy_id == "RAID-C4"
    # RSI 55 is above the new ceiling → still no trade.
    assert c4.generate_candidates(_ctx(MarketRegime.RANGE, features={"5m": _c4_feat(55.0)})) == []


# ── 5-MINUTE CYCLE changes ───────────────────────────────────────────────────

def test_cycle_time_is_five_minutes():
    assert config.BRAIN_CYCLE_MINUTES == 5


def test_ohlcv_refresh_schedule():
    # 5m refreshes every cycle; 15m only every 3rd; 1h every 12th; a cold entry always fetches.
    assert _refresh_due("5m", 2, False) is True
    assert _refresh_due("5m", 7, False) is True
    assert _refresh_due("15m", 3, False) is True
    assert _refresh_due("15m", 2, False) is False
    assert _refresh_due("30m", 6, False) is True
    assert _refresh_due("30m", 5, False) is False
    assert _refresh_due("1h", 12, False) is True
    assert _refresh_due("1h", 6, False) is False
    assert _refresh_due("1h", 5, True) is True          # cold → always fetch


def test_fg_cache_freshness():
    now = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
    assert _cache_fresh(now - timedelta(minutes=10), now, 30) is True     # within 30m
    assert _cache_fresh(now - timedelta(minutes=40), now, 30) is False    # older than 30m
    assert _cache_fresh(None, now, 30) is False


def test_symbol_cooldown_15min():
    # 15 min = 0.25h. Re-entry blocked within the window, allowed after it.
    assert within_cooldown("2026-07-03T00:00:00+00:00", "2026-07-03T00:10:00+00:00", 15 / 60.0) is True
    assert within_cooldown("2026-07-03T00:00:00+00:00", "2026-07-03T00:20:00+00:00", 15 / 60.0) is False


def test_regime_cleanup_runs_each_cycle():
    from raid.runner import run_strategy_cycle
    db = _FullBookDB([])
    scans = [_cycle_scan(f"S{k}USD") for k in range(3)]
    asyncio.run(run_strategy_cycle(scans, db, {}))
    assert db.cleanup_calls == [48]   # called once per cycle, at the start, with 48h retention


# ── ENABLE ALL STRATEGIES: shorts (C3, C7), pairs/carry (C8/C9), opposite-dir ──

def test_c3_generates_short_with_correct_geometry():
    from raid.strategies.trend import C3ShortTrendBreakdown
    feat = _feat("5m", last_price=100.0, swing_low=100.0, swing_high=110.0,
                 ema20=99.0, ema50=101.0, atr_pct=0.01)
    ctx = _ctx(MarketRegime.TREND_DOWN, features={"5m": feat}, caps=frozenset({CAP_SHORT}))
    cands = C3ShortTrendBreakdown().generate_candidates(ctx)
    assert len(cands) == 1
    c = cands[0]
    assert c.direction == Direction.SHORT and c.strategy_id == "RAID-C3"
    assert float(c.stop_price) > float(c.reference_price)   # short SL ABOVE entry
    assert float(c.targets[0]) < float(c.reference_price)   # short TP BELOW entry


def test_c7_short_flag_gated_to_trend_down():
    # C7 shorts are gated to TREND_DOWN (mirror C3) AND to config.C7_SHORT_ENABLED. TREND_DOWN is
    # now ELIGIBLE (so the short is reachable); the flag decides book (paper) vs shadow-only.
    # Enabling reversed the deliberate ~-$33 C7-short-in-RANGE bleed gate (see rotation.py).
    import config
    from raid.strategies.rotation import C7CrossSectionalMomentum
    c7 = C7CrossSectionalMomentum()
    laggard = {"universe_rankings": {"SOLUSD": _rank(10, 10, ret=-0.05, ram=-0.5)}}
    # RANGE: never a live short regardless of the flag (short branch requires TREND_DOWN) -> shadow.
    ctx_r = _ctx(MarketRegime.RANGE, extras=laggard, caps=frozenset({CAP_SPOT_LONG, CAP_SHORT}))
    assert c7.generate_candidates(ctx_r) == []
    assert ctx_r.extras.get("_c7_shadow_shorts")
    # TREND_DOWN is now eligible -> the short is reachable (the enabling change).
    ctx_d = _ctx(MarketRegime.TREND_DOWN, extras=laggard, caps=frozenset({CAP_SPOT_LONG, CAP_SHORT}))
    assert c7.is_eligible(ctx_d) is True
    try:
        config.C7_SHORT_ENABLED = True
        cands = c7.generate_candidates(ctx_d)
        assert len(cands) == 1 and cands[0].direction == Direction.SHORT and cands[0].strategy_id == "RAID-C7"
        # Flag OFF -> shadow-only even in TREND_DOWN (independently killable).
        config.C7_SHORT_ENABLED = False
        ctx_off = _ctx(MarketRegime.TREND_DOWN, extras=laggard, caps=frozenset({CAP_SPOT_LONG, CAP_SHORT}))
        assert c7.generate_candidates(ctx_off) == []
        assert ctx_off.extras.get("_c7_shadow_shorts")
    finally:
        config.C7_SHORT_ENABLED = True


def test_opposite_direction_protection():
    from raid.core.universe import has_opposite
    assert has_opposite({"long"}, "short") is True     # short blocked when a long is open
    assert has_opposite({"short"}, "long") is True      # long blocked when a short is open
    assert has_opposite({"short"}, "short") is False    # same-direction stacking allowed
    assert has_opposite(set(), "short") is False


class _CaptureDB:
    """Captures the SL persisted by update_trailing_stop (mocks the supabase chain)."""
    def __init__(self):
        self.persisted_sl = None
        self.supabase = self

    def table(self, name): return self
    def update(self, d): self._sl = d.get("sl"); return self
    def eq(self, k, v): return self
    async def execute(self): self.persisted_sl = self._sl; return None


def test_executor_short_sl_tp_hit():
    from executor import _sl_tp_hit
    assert _sl_tp_hit("short", 102.0, 102.0, 95.0) == "stop_loss"    # price >= sl (SL above entry)
    assert _sl_tp_hit("short", 95.0, 102.0, 95.0) == "take_profit"   # price <= tp (TP below entry)
    assert _sl_tp_hit("short", 99.0, 102.0, 95.0) is None


def test_executor_short_pnl():
    from executor import compute_pnl
    assert compute_pnl("short", 100.0, 95.0, 200.0) > 0     # exit below entry -> profit
    assert compute_pnl("short", 100.0, 105.0, 200.0) < 0    # exit above entry -> loss


def test_executor_short_trail_moves_down():
    from executor import update_trailing_stop
    db = _CaptureDB()
    # Short entry 100, price fell to 97 (+3% for a short) -> trail locks SL BELOW entry.
    trade = {"id": "x", "direction": "short", "entry_price": 100.0, "sl": 102.0, "symbol": "T"}
    asyncio.run(update_trailing_stop(trade, 97.0, db))
    assert db.persisted_sl is not None and db.persisted_sl < 100.0   # moved DOWN, not up


# ── 3X LEVERAGE with drawdown de-risking ─────────────────────────────────────

def test_effective_leverage_ladder():
    from raid.runner import _effective_leverage
    assert _effective_leverage(0.00) == (3, None)          # normal 3x
    assert _effective_leverage(0.05) == (3, None)          # <6% stays 3x
    assert _effective_leverage(0.07) == (2, None)          # 6%+  -> 2x
    assert _effective_leverage(0.11) == (1, None)          # 10%+ -> 1x
    assert _effective_leverage(0.16) == (None, "pause")    # 15%+ -> pause
    assert _effective_leverage(0.21) == (None, "shutdown") # 20%+ -> shutdown


def test_leverage_sizing_math():
    base = 4000 * config.MAX_TRADE_SIZE_PCT                  # $200 base (margin)
    assert base == 200.0
    notional = base * config.LEVERAGE_MULTIPLIER             # $600 notional at 3x
    assert notional == 600.0
    assert notional / config.LEVERAGE_MULTIPLIER == 200.0    # margin recovered


def test_trade_margin_parsing():
    from raid.core.universe import trade_margin
    # Leveraged trade tags margin -> parsed (not the $600 notional).
    assert trade_margin({"claude_reasoning": "RAID-C2 limit net_rr=1.9 lev=3x margin=200.00 :: x",
                         "size_usd": 600.0}) == 200.0
    # Pre-leverage / untagged trade -> notional (size_usd) IS the margin.
    assert trade_margin({"claude_reasoning": "RAID-C2 limit net_rr=1.9 :: x", "size_usd": 200.0}) == 200.0


def test_pnl_uses_notional_at_leverage():
    import costs
    from executor import compute_pnl
    # $600 notional, +2% move -> $12 gross minus the real all-in round-trip cost
    # (~1.04% of notional = ~$6.24) -> ~$5.76 net. Uses NOTIONAL, both legs.
    pnl = compute_pnl("long", 100.0, 102.0, 600.0)
    expected = 12.0 - 600.0 * costs.realized_round_trip_cost_pct()
    assert abs(pnl - expected) < 1e-6, (pnl, expected)


def test_deployment_cap_counts_margin_allows_19():
    from raid.core.universe import trade_margin
    trades = [{"claude_reasoning": "RAID-C2 x lev=3x margin=200.00 :: y", "size_usd": 600.0} for _ in range(19)]
    total_margin = sum(trade_margin(t) for t in trades)
    assert total_margin == 3800.0                 # 19 x $200 margin ($11,400 notional)
    assert total_margin <= 4000 * 0.95            # fits the 95% cap
    assert total_margin + 200.0 > 4000 * 0.95     # a 20th would exceed


# ── C1 un-quarantine: 1.5x breakout-volume confirmation ──────────────────────

def _c1_feat():
    # Valid C1 breakout setup: stacked up, price just under resistance.
    return _feat("5m", last_price=99.5, swing_high=100.0, ema20=99.0, ema50=98.0, atr_pct=0.008)


def _vol_candles(last_vol, avg_vol=100.0, n=21):
    rows = [[i, 99.0, 99.0, 99.0, 99.0, avg_vol] for i in range(n - 1)]
    rows.append([n, 99.0, 99.0, 99.0, 99.0, last_vol])
    return rows


def test_c1_volume_filter_skips_low_volume():
    from raid.strategies.trend import C1LongTrendBreakout
    ctx = _ctx(MarketRegime.TREND_UP, extras={"candles_5m": _vol_candles(120.0)}, features={"5m": _c1_feat()})
    assert C1LongTrendBreakout().generate_candidates(ctx) == []   # 1.2x avg < 1.5x -> skip


def test_c1_volume_filter_allows_high_volume():
    from raid.strategies.trend import C1LongTrendBreakout
    ctx = _ctx(MarketRegime.TREND_UP, extras={"candles_5m": _vol_candles(200.0)}, features={"5m": _c1_feat()})
    cands = C1LongTrendBreakout().generate_candidates(ctx)
    assert len(cands) == 1 and cands[0].strategy_id == "RAID-C1"   # 2.0x avg >= 1.5x -> produce


# ── regression: a full book must NOT blank out regime logging ────────────────

def _cycle_scan(sym):
    from scanner import ScanResult
    c5 = [[1_700_000_000 + i * 300, 100 + i * 0.1, 100 + i * 0.1 + 0.05, 100 + i * 0.1 - 0.05, 100 + i * 0.1, 20.0] for i in range(40)]
    c1h = [[1_700_000_000 + i * 3600, 100 + i * 0.2, 100 + i * 0.2 + 0.1, 100 + i * 0.2 - 0.1, 100 + i * 0.2, 20.0] for i in range(40)]
    last = c5[-1][4]
    return ScanResult(
        market="crypto", symbol=sym, ohlcv=c5, ohlcv_15m=c1h, ohlcv_30m=c1h, ohlcv_1h=c1h,
        current_price=last, scan_time="2026-07-03T00:00:00+00:00",
        order_book={"bid_walls": [{"price": last * 0.999, "usd": 5000.0}],
                    "ask_walls": [{"price": last * 1.001, "usd": 3000.0}]},
    )


class _FullBookDB:
    def __init__(self, open_trades):
        self._open = open_trades
        self.regimes = []
        self.trades = []
        self.cleanup_calls = []

    async def try_claim_lease(self, *a): return True
    async def get_equity(self): return 4000.0
    async def get_open_trades(self): return list(self._open)
    async def get_closed_trades_last_n(self, n): return []
    async def get_open_trades_by_market(self, m): return []
    async def get_kill_switch(self): return False
    async def get_daily_stats(self, d): return {"pnl": 0}
    async def get_consecutive_losses(self): return 0   # circuit breaker: no streak -> no pause
    async def get_realized_equity(self): return 4000.0
    async def get_daily_equity_base(self): return 4000.0
    async def update_operator_controls(self, updates): return True
    async def log_regime(self, e): self.regimes.append(e)
    async def log_trade(self, t): self.trades.append(t); return f"f{len(self.trades)}"
    async def close_trade(self, *a): pass
    async def cleanup_regime_log(self, hours): self.cleanup_calls.append(hours); return 0


def test_full_book_still_logs_regimes():
    # Reproduces the 2026-07-03 incident: the book at the 95% deployment cap
    # (19 x $200 = $3800 of $4000) must still classify + log a regime for EVERY symbol,
    # even though it (correctly) books no new trades. Before the fix the loop broke on
    # the first symbol and logged zero regimes.
    from raid.runner import run_strategy_cycle
    full = [{"id": f"o{i}", "symbol": f"X{i}USD", "size_usd": 200.0, "direction": "long",
             "claude_reasoning": "RAID-C2 limit net_rr=1.5 :: x",
             "open_time": "2026-07-03T00:00:00+00:00"} for i in range(19)]
    scans = [_cycle_scan(f"S{k}USD") for k in range(6)]
    db = _FullBookDB(full)
    booked = asyncio.run(run_strategy_cycle(scans, db, {}))
    assert booked == 0, f"a 95%-deployed book must book no new trades, got {booked}"
    assert len(db.regimes) == len(scans), f"regimes must be logged for all symbols when full, got {len(db.regimes)}"
