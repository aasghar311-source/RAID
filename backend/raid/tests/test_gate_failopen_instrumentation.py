"""B0.5 — measure-first instrumentation of gate.check_gate's swallowed exceptions.

Proves the instrumentation (a) logs GATE_FAILOPEN + GATE_PASSED_ON_SWALLOW when a check's
exception is swallowed, (b) does NOT change behavior (the gate still passes / rejects exactly
as before), and (c) stays silent when nothing is swallowed. No network, no DB, no enforcement
flip. Auto-discovered by raid.tests.run_all.
"""

import asyncio
import logging

import gate


class _Sig:
    """Minimal signal stand-in — check_gate only reads .market; instrumentation reads
    .symbol/.market/.direction via getattr."""
    market = "crypto"
    symbol = "TESTUSD"
    direction = "long"


class _DB:
    """Async fake DB; optionally raises on a named call to exercise a swallow."""

    def __init__(self, raise_on=None, kill=False):
        self._raise_on = set(raise_on or ())
        self._kill = kill

    async def get_kill_switch(self):
        if "kill" in self._raise_on:
            raise RuntimeError("kill boom")
        return self._kill

    async def get_equity(self):
        if "equity" in self._raise_on:
            raise RuntimeError("equity boom")
        return 4000.0

    async def get_daily_stats(self, _today):
        return {"pnl": 0}

    async def set_kill_switch(self, *_a, **_k):
        return True

    async def get_open_trades(self):
        return []

    async def get_open_trades_by_market(self, _market):
        return []


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


def _run_with_capture(db):
    cap = _Capture()
    lg = logging.getLogger("raid.gate")
    lg.addHandler(cap)
    prev = lg.level
    lg.setLevel(logging.WARNING)
    try:
        result = asyncio.run(gate.check_gate(_Sig(), db))
    finally:
        lg.removeHandler(cap)
        lg.setLevel(prev)
    return result, cap.msgs


def test_swallowed_exception_is_instrumented_and_behavior_unchanged():
    result, msgs = _run_with_capture(_DB(raise_on={"kill"}))
    # behavior UNCHANGED — a swallowed kill-switch error still lets the gate pass
    assert result.passed is True and result.reason == "all_checks_passed"
    # instrumentation fired
    assert any("GATE_FAILOPEN" in m and "check=kill_switch" in m and "would_reject_failclosed=1" in m
               for m in msgs), msgs
    assert any("GATE_PASSED_ON_SWALLOW" in m and "kill_switch" in m for m in msgs), msgs


def test_clean_gate_emits_no_instrumentation():
    result, msgs = _run_with_capture(_DB())
    assert result.passed is True and result.reason == "all_checks_passed"
    assert not any("GATE_FAILOPEN" in m or "GATE_PASSED_ON_SWALLOW" in m for m in msgs), msgs


def test_legit_reject_unaffected_and_no_swallow_log():
    # kill switch genuinely ON (no exception) -> reject, and no swallow instrumentation.
    result, msgs = _run_with_capture(_DB(kill=True))
    assert result.passed is False and result.reason == "kill_switch_active"
    assert not any("GATE_FAILOPEN" in m or "GATE_PASSED_ON_SWALLOW" in m for m in msgs), msgs
