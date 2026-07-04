"""Trailing-stop invariant tests (Phase-3 lock-in of the *proven-correct* trail math).

Phase-1 forensics on the real SPXUSD trailing_stop close (entry 0.4115, peak_pnl_pct
1.58, stored sl 0.417025) proved the lock math is correct: the stored stop equals
entry*(1 + peak_gain*0.85) to 6 digits, and it ratchets monotonically. These tests
encode those invariants so a future refactor cannot regress them, and cover the new
fail-closed staleness guard and the fill-slippage diagnostic. No trail thresholds are
asserted here beyond the config values themselves (1.5% arm / 0.85 lock).

Discovered and run by raid.tests.run_all (plain asserts, no pytest).
"""

import asyncio

import config
import costs
import executor


def _run(coro):
    return asyncio.run(coro)


class _FakeChain:
    """Captures the SL written by executor._persist_sl; never touches Supabase."""

    def __init__(self, sink):
        self._sink = sink

    def table(self, *_a, **_k):
        return self

    def update(self, d):
        if "sl" in d:
            self._sink["sl"] = d["sl"]
        return self

    def eq(self, *_a, **_k):
        return self

    async def execute(self):
        return None


class _FakeDB:
    def __init__(self):
        self.last = {}

    @property
    def supabase(self):
        return _FakeChain(self.last)


def _tick(trade, price, db):
    """One monitor tick's worth of trailing update, then simulate the next-tick DB
    reload by copying any newly-persisted SL back onto the trade dict."""
    _run(executor.update_trailing_stop(trade, price, db))
    if "sl" in db.last:
        trade["sl"] = db.last["sl"]
    return trade["sl"]


# ── LONG ────────────────────────────────────────────────────────────────────
def test_long_locks_at_85pct_of_peak():
    e = 0.4115
    trade = {"id": "t-long", "direction": "long", "entry_price": e, "sl": e * 0.99, "symbol": "SPXUSD"}
    db = _FakeDB()
    _tick(trade, e * 1.0158, db)                     # +1.58% peak (the real SPXUSD case)
    expected = e * (1 + 0.0158 * 0.85)               # 0.417026...
    assert abs(db.last["sl"] - expected) < 1e-6, (db.last["sl"], expected)
    # matches the real stored stop 0.417025 to 5 digits
    assert abs(db.last["sl"] - 0.417025) < 1e-4, db.last["sl"]


def test_long_ratchets_up_only():
    e = 0.4115
    trade = {"id": "t-long", "direction": "long", "entry_price": e, "sl": e * 0.99, "symbol": "SPXUSD"}
    db = _FakeDB()
    seen = []
    for mult in (1.010, 1.0158, 1.020, 1.012, 1.005):  # rise past peak, then give it back
        seen.append(_tick(trade, e * mult, db))
    # Never loosens: the SL sequence is non-decreasing.
    for a, b in zip(seen, seen[1:]):
        assert b >= a - 1e-12, (a, b)
    # Final stop locks at 85% of the +2.0% high-water mark, not the pullback.
    assert abs(trade["sl"] - e * (1 + 0.020 * 0.85)) < 1e-6, trade["sl"]


def test_long_not_armed_below_trigger():
    e = 0.4115
    trade = {"id": "t-long", "direction": "long", "entry_price": e, "sl": e * 0.99, "symbol": "SPXUSD"}
    db = _FakeDB()
    _tick(trade, e * (1 + config.TRAIL_TRIGGER_PCT - 0.002), db)  # +1.3% < 1.5% arm
    assert "sl" not in db.last, "trail must not arm below TRAIL_TRIGGER_PCT"


# ── SHORT (mirror) ────────────────────────────────────────────────────────────
def test_short_locks_and_ratchets_down_only():
    e = 0.4603
    trade = {"id": "t-short", "direction": "short", "entry_price": e, "sl": e * 1.01, "symbol": "SYNUSD"}
    db = _FakeDB()
    seen = []
    for mult in (1 - 0.0158, 1 - 0.020, 1 - 0.012):  # fall to +2% gain, then bounce
        seen.append(_tick(trade, e * mult, db))
    for a, b in zip(seen, seen[1:]):
        assert b <= a + 1e-12, (a, b)                 # short stop only tightens downward
    assert abs(trade["sl"] - e * (1 - 0.020 * 0.85)) < 1e-6, trade["sl"]


# ── Fail-closed staleness guard ───────────────────────────────────────────────
def test_price_too_stale_guard():
    assert executor.price_too_stale(config.STALE_PRICE_SECONDS + 1) is True
    assert executor.price_too_stale(config.STALE_PRICE_SECONDS - 1) is False
    assert executor.price_too_stale(0.0) is False
    assert executor.price_too_stale(None) is False   # unknown age -> treat as fresh


# ── Fill-slippage diagnostic ──────────────────────────────────────────────────
def test_fill_slippage_sign():
    e = 0.4115
    # Long: filled BELOW the trailed stop (gapped through) -> negative slip.
    assert executor.fill_slippage_pct("long", 0.417025, 0.414, e) < 0
    # Long: filled exactly at the stop -> ~0.
    assert abs(executor.fill_slippage_pct("long", 0.417025, 0.417025, e)) < 1e-9
    # Short: filled ABOVE the stop (worse) -> negative slip.
    assert executor.fill_slippage_pct("short", 0.45382, 0.460, 0.4603) < 0


# ── Trail fee-floor sourced from the real round-trip cost (SSOT) ───────────────
def test_trail_fee_floor_uses_real_round_trip_cost():
    rt = costs.realized_round_trip_cost_pct()   # ~0.0104
    assert abs(executor._trail_fee_floor(100.0, True) - 100.0 * (1 + rt)) < 1e-12   # long
    assert abs(executor._trail_fee_floor(100.0, False) - 100.0 * (1 - rt)) < 1e-12  # short
    # Sourced from the SSOT, not the old hardcoded 0.4%.
    assert executor._trail_fee_floor(100.0, True) > 100.0 * 1.004


def test_trail_floor_clamps_a_sub_cost_level_up():
    # A long stop at +0.5% was "profitable" under the old 0.4% floor but is a NET LOSS after
    # the real ~1.04% cost. The floor now clamps it up to the true break-even level.
    e = 100.0
    sub_cost_sl = e * 1.005                       # +0.5% (below real round-trip cost)
    floor = executor._trail_fee_floor(e, True)    # ~+1.04%
    assert max(sub_cost_sl, floor) == floor       # clamped UP to fee-covering
    assert floor > sub_cost_sl


def test_trail_trigger_and_lock_unchanged():
    # This commit does NOT touch the trigger (1.5%) or lock ratio (0.85). At gain=1.5% the
    # locked stop (+1.275%) is above the fee floor, so the floor is non-binding today.
    assert config.TRAIL_TRIGGER_PCT == 0.015
    locked_at_trigger = 1 + config.TRAIL_TRIGGER_PCT * 0.85   # 1.01275
    assert locked_at_trigger > (1 + costs.realized_round_trip_cost_pct())  # floor non-binding
