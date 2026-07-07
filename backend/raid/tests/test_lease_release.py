"""Lease-release fix — db.release_lease frees ONLY this worker's lease on shutdown (no PASSIVE gap
on the next deploy). Fake async Supabase client (db.supabase is a swappable module global). Plain
asserts. Auto-discovered by raid.tests.run_all.
"""

import asyncio

import db


class _Chain:
    def __init__(self, rec):
        self.rec = rec

    def update(self, fields):
        self.rec["update"] = dict(fields)
        return self

    def eq(self, k, v):
        self.rec.setdefault("eq", []).append((k, v))
        return self

    async def execute(self):
        self.rec["executed"] = True
        return type("R", (), {"data": [{"id": 1}]})()


class _Client:
    def __init__(self, rec):
        self.rec = rec

    def table(self, name):
        self.rec["table"] = name
        return _Chain(self.rec)


def test_release_lease_clears_only_this_worker():
    rec: dict = {}
    orig = db.supabase
    db.supabase = _Client(rec)
    try:
        ok = asyncio.run(db.release_lease("worker-X"))
    finally:
        db.supabase = orig
    assert ok is True
    assert rec["table"] == "worker_leases"
    assert rec["update"].get("holder_id") is None            # frees the lease
    assert ("id", 1) in rec["eq"]                             # the single lease row
    assert ("holder_id", "worker-X") in rec["eq"]            # scoped to THIS worker only
