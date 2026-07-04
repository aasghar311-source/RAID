"""Automated-backstop tests (Commit E): with the consecutive-loss pause REMOVED, the only
automated backstops are the drawdown de-risk ladder and the manual kill_switch. Both must
remain functional.

Discovered and run by raid.tests.run_all (plain asserts, no pytest).
"""

import asyncio

import worker
from raid.runner import _effective_leverage


def test_drawdown_ladder_triggers_at_each_threshold():
    # 6%->2x, 10%->1x, 15%->pause entries, 20%->hard shutdown (config.LEVERAGE_DERISKING).
    assert _effective_leverage(0.00) == (3, None)          # full leverage
    assert _effective_leverage(0.05) == (3, None)          # below 6% -> still 3x
    assert _effective_leverage(0.06) == (2, None)          # 6% -> 2x
    assert _effective_leverage(0.09) == (2, None)
    assert _effective_leverage(0.10) == (1, None)          # 10% -> 1x
    assert _effective_leverage(0.15) == (None, "pause")    # 15% -> pause all entries
    assert _effective_leverage(0.20) == (None, "shutdown") # 20% -> hard shutdown
    assert _effective_leverage(0.35) == (None, "shutdown")


class _FakeDB:
    async def log_regime(self, e):
        return None


def test_kill_switch_and_manual_pause_block_entries():
    db = _FakeDB()
    # Manual kill switch is absolute; manual pause blocks entries; neither -> entries allowed.
    assert asyncio.run(worker._brain_entry_gate(db, {"kill_switch": True})) is False
    assert asyncio.run(worker._brain_entry_gate(db, {"pause_entries": True})) is False
    assert asyncio.run(worker._brain_entry_gate(db, {})) is True
