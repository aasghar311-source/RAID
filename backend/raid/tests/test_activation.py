"""Tests for the C6/C7/C10 activation: universe ranking, sweep detection, the two
cross-sectional long strategies, the liquidity-sweep strategy, the C6 rebalance limiter,
and the no-stacking (C7-vs-C6 dedupe) gate."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from raid.core.candidate import Direction, MarketRegime
from raid.core.features import FeatureSnapshot
from raid.core.microstructure import detect_liquidity_sweep
from raid.core.provider import CAP_SPOT_LONG
from raid.core.strategy import StrategyContext
from raid.core.universe import compute_universe_rankings, parse_strategy_tag, within_cooldown
from raid.execution.time_stops import C10_MAX_HOLD_MINUTES, c10_time_stop_due
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
    ex = {"equity": 10000.0, "risk_pct": 0.005, "expiry_ts": "2026-07-02T00:20:00Z"}
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

    async def try_claim_lease(self, *a): return True
    async def get_equity(self): return 4000.0
    async def get_open_trades(self): return list(self._open)
    async def get_closed_trades_last_n(self, n): return []
    async def get_open_trades_by_market(self, m): return []
    async def get_kill_switch(self): return False
    async def get_daily_stats(self, d): return {"pnl": 0}
    async def log_regime(self, e): self.regimes.append(e)
    async def log_trade(self, t): self.trades.append(t); return f"f{len(self.trades)}"
    async def close_trade(self, *a): pass


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
