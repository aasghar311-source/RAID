"""Regression guard for db.update_equity (Bug B): the equity_snapshots insert must write
ONLY columns that exist (equity, daily_pnl) and NOT paper_mode, which the table never had
(the write failed PGRST204 since the initial deploy, leaving the table empty). Fake async
client, plain asserts."""

import asyncio
import db


class _Resp:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, rec):
        self._rec = rec

    async def execute(self):
        self._rec["executed"] = True
        return _Resp([])


class _Table:
    def __init__(self, rec):
        self._rec = rec

    def insert(self, row):
        self._rec["insert_row"] = row
        return _Q(self._rec)


class _Client:
    def __init__(self, rec):
        self._rec = rec

    def table(self, name):
        self._rec["table"] = name
        return _Table(self._rec)


def test_update_equity_writes_only_existing_columns():
    rec: dict = {}
    orig = db.supabase
    db.supabase = _Client(rec)
    try:
        asyncio.run(db.update_equity(4321.0, 12.5))
    finally:
        db.supabase = orig
    assert rec["table"] == "equity_snapshots"
    assert rec["insert_row"] == {"equity": 4321.0, "daily_pnl": 12.5}, rec["insert_row"]
    assert "paper_mode" not in rec["insert_row"]   # the column does not exist -> would 400
    assert rec.get("executed") is True             # insert was awaited
