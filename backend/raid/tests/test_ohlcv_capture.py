"""OHLCV capture (migration 004 / Option B) — write-only backtest instrumentation.

Proves: (1) the pure row-builder maps the in-memory candle fields to the exact ohlcv_5m
columns (no mis-indexing, no fabrication); (2) the async writer NEVER raises — a simulated
capture-write failure returns 0 and is swallowed, so it cannot propagate into the trade
cycle; (3) it no-ops when disabled (fail-closed) and self-disables when the table is absent.

Plain asserts + asyncio.run so raid.tests.run_all discovers them. No real DB — supabase is
replaced with a fake, and db module globals are saved/restored around each test.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import db

TOL = 1e-9


# ── fake async supabase client ──────────────────────────────────────────────
class _FakeQuery:
    def __init__(self, raises):
        self._raises = raises

    async def execute(self):
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(data=[])


class _FakeTable:
    def __init__(self, raises):
        self._raises = raises
        self.upsert_calls = []            # list of (rows, on_conflict)

    def upsert(self, rows, on_conflict=None):
        self.upsert_calls.append((rows, on_conflict))
        return _FakeQuery(self._raises)


class _FakeSupabase:
    def __init__(self, raises=None):
        self._table = _FakeTable(raises)
        self.tables_seen = []

    def table(self, name):
        self.tables_seen.append(name)
        return self._table


def _with_db_state(enabled, supabase, ok=True):
    """Save current db globals, apply overrides, return a restore() thunk."""
    saved = (db.OHLCV_CAPTURE_ENABLED, db.supabase, db._ohlcv_capture_ok)
    db.OHLCV_CAPTURE_ENABLED = enabled
    db.supabase = supabase
    db._ohlcv_capture_ok = ok

    def restore():
        db.OHLCV_CAPTURE_ENABLED, db.supabase, db._ohlcv_capture_ok = saved
    return restore


# ── (1) pure row-builder maps the right in-memory values ────────────────────
def test_build_rows_maps_ohlcv_fields_and_tail():
    ts = 1_720_130_400   # a fixed epoch (no Date.now); distinct o/h/l/c/v catch mis-indexing
    bars = [
        [ts - 300, 1, 1, 1, 1, 1],                     # older bar — excluded by tail=2
        [ts - 0,   100.0, 105.0, 95.0, 102.0, 5000.0], # will be captured
        [ts + 300, 200.0, 210.0, 190.0, 205.0, 9000.0],# will be captured
    ]
    rows = db.build_ohlcv_capture_rows("ETHUSD", bars, tail=2)
    assert len(rows) == 2                              # only the last 2 bars
    r = rows[0]
    assert r["symbol"] == "ETHUSD"
    assert r["open"] == 100.0 and r["high"] == 105.0 and r["low"] == 95.0
    assert r["close"] == 102.0 and r["volume"] == 5000.0
    # bar_ts is the candle open time (bar[0]) as UTC ISO — matches the same conversion.
    assert r["bar_ts"] == datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def test_build_rows_skips_malformed_and_empty():
    assert db.build_ohlcv_capture_rows("X", None) == []
    assert db.build_ohlcv_capture_rows("X", []) == []
    # short/garbage candles are skipped, not fabricated
    assert db.build_ohlcv_capture_rows("X", [[1, 2, 3]], tail=2) == []
    assert db.build_ohlcv_capture_rows("X", [[1, "bad", 3, 4, 5]], tail=2) == []


# ── (2) a capture-write exception NEVER propagates into the trade cycle ──────
def test_capture_swallows_transient_error_and_keeps_enabled():
    restore = _with_db_state(True, _FakeSupabase(raises=RuntimeError("boom 503")))
    try:
        rows = [{"symbol": "ETHUSD", "bar_ts": "2026-07-04T22:00:00+00:00",
                 "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
        out = asyncio.run(db.capture_ohlcv_5m(rows))   # MUST NOT raise
        assert out == 0
        assert db._ohlcv_capture_ok is True            # transient -> stays enabled, retries next cycle
    finally:
        restore()


def test_capture_self_disables_when_table_absent():
    err = RuntimeError("{'code': 'PGRST205', 'message': 'Could not find the table ohlcv_5m'}")
    fake = _FakeSupabase(raises=err)
    restore = _with_db_state(True, fake)
    try:
        out = asyncio.run(db.capture_ohlcv_5m([{"symbol": "X", "bar_ts": "t"}]))
        assert out == 0
        assert db._ohlcv_capture_ok is False           # table-absent -> permanently no-ops this process
    finally:
        restore()


# ── (3) fail-closed when disabled; happy path calls upsert with the conflict key ─
def test_capture_noop_when_disabled():
    fake = _FakeSupabase()
    restore = _with_db_state(False, fake)               # OHLCV_CAPTURE_ENABLED = False
    try:
        out = asyncio.run(db.capture_ohlcv_5m([{"symbol": "X", "bar_ts": "t"}]))
        assert out == 0
        assert fake._table.upsert_calls == []           # no write attempted at all
    finally:
        restore()


def test_capture_happy_path_upserts_with_conflict_key():
    fake = _FakeSupabase(raises=None)
    restore = _with_db_state(True, fake)
    try:
        rows = [{"symbol": "ETHUSD", "bar_ts": "2026-07-04T22:00:00+00:00",
                 "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
        out = asyncio.run(db.capture_ohlcv_5m(rows))
        assert out == 1
        assert fake.tables_seen == ["ohlcv_5m"]
        sent_rows, on_conflict = fake._table.upsert_calls[0]
        assert sent_rows == rows and on_conflict == "symbol,bar_ts"
    finally:
        restore()


def test_capture_noop_on_empty_rows():
    fake = _FakeSupabase()
    restore = _with_db_state(True, fake)
    try:
        assert asyncio.run(db.capture_ohlcv_5m([])) == 0
        assert fake._table.upsert_calls == []
    finally:
        restore()
