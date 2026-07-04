"""Regression guard for db.close_trade (Bug A): every DB call in the close path must be
AWAITED, and the primary update must actually fire. Uses a fake async Supabase client
(db.supabase is a lazily-created module global, so we can swap it). Plain asserts."""

import asyncio
import db


class _Resp:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, rec, kind):
        self._rec = rec
        self._kind = kind

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    async def execute(self):
        self._rec.setdefault("executed", []).append(self._kind)
        # the open_time SELECT returns a row so hold_minutes can be computed
        return _Resp([{"open_time": "2026-07-04T00:00:00+00:00"}] if self._kind == "select" else [])


class _Table:
    def __init__(self, rec):
        self._rec = rec

    def select(self, *a, **k):
        return _Q(self._rec, "select")

    def update(self, fields):
        self._rec["update_fields"] = fields
        return _Q(self._rec, "update")


class _Client:
    def __init__(self, rec):
        self._rec = rec

    def table(self, name):
        self._rec.setdefault("tables", []).append(name)
        return _Table(self._rec)


def _run_close(**over):
    rec: dict = {}
    orig = db.supabase
    db.supabase = _Client(rec)
    try:
        asyncio.run(db.close_trade("tid-1", 1.2345, 4.5, "take_profit", **over))
    finally:
        db.supabase = orig
    return rec


def test_close_trade_awaits_primary_update_and_persists():
    f = _run_close()["update_fields"]
    assert f["status"] == "closed"
    assert f["close_reason"] == "take_profit"
    assert f["pnl"] == 4.5
    assert "close_time" in f


def test_close_trade_awaits_select_so_hold_minutes_is_captured():
    # Bug A regression: if the open_time SELECT is not awaited, _row is a coroutine,
    # _row.data raises (swallowed), and hold_minutes is dropped. Its presence proves the await.
    rec = _run_close()
    assert "select" in rec.get("executed", []) and "update" in rec.get("executed", [])
    assert "hold_minutes" in rec["update_fields"], "hold_minutes missing -> the SELECT was not awaited (Bug A regressed)"
    assert rec["update_fields"]["hold_minutes"] > 0


def test_close_trade_merges_extra_into_same_update():
    f = _run_close(extra={"regime_at_exit": "range"})["update_fields"]
    assert f["regime_at_exit"] == "range"
    assert f["status"] == "closed"   # extra merges alongside the primary fields
