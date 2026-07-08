"""B0.5 instrumentation + B.3 fail-closed enforcement of gate.check_gate's swallowed exceptions.

Proves the instrumentation (a) logs GATE_FAILOPEN + GATE_PASSED_ON_SWALLOW when a check's
exception is swallowed (in BOTH modes), (b) behavior is flag-gated: with ENFORCE_GATE_FAIL_CLOSED
False the gate still passes on a swallow (B0.5 measure-first), with it True the same swallow
REJECTS (B.3 fail-closed), and (c) stays silent when nothing is swallowed. No network, no DB.
Auto-discovered by raid.tests.run_all.
"""

import asyncio
import logging

import config
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


def _run_with_capture(db, strategy=None, cycle_ts=None):
    cap = _Capture()
    lg = logging.getLogger("raid.gate")
    lg.addHandler(cap)
    prev = lg.level
    lg.setLevel(logging.WARNING)
    try:
        result = asyncio.run(gate.check_gate(_Sig(), db, strategy=strategy, cycle_ts=cycle_ts))
    finally:
        lg.removeHandler(cap)
        lg.setLevel(prev)
    return result, cap.msgs


def test_swallowed_exception_instrumented_and_flag_gated_behavior():
    _prev = config.ENFORCE_GATE_FAIL_CLOSED
    try:
        # fail-OPEN (flag off, B0.5): a swallowed kill-switch error still lets the gate pass.
        config.ENFORCE_GATE_FAIL_CLOSED = False
        result, msgs = _run_with_capture(_DB(raise_on={"kill"}))
        assert result.passed is True and result.reason == "all_checks_passed"
        assert any("GATE_FAILOPEN" in m and "check=kill_switch" in m and "would_reject_failclosed=1" in m
                   for m in msgs), msgs
        assert any("GATE_PASSED_ON_SWALLOW" in m and "kill_switch" in m for m in msgs), msgs
        # fail-CLOSED (flag on, B.3): the SAME swallow now REJECTS; instrumentation still fires.
        config.ENFORCE_GATE_FAIL_CLOSED = True
        result2, msgs2 = _run_with_capture(_DB(raise_on={"kill"}))
        assert result2.passed is False and result2.reason == "gate_failclosed_on_swallow"
        assert any("GATE_FAILOPEN" in m and "check=kill_switch" in m for m in msgs2), msgs2
        assert any("GATE_PASSED_ON_SWALLOW" in m and "kill_switch" in m for m in msgs2), msgs2
    finally:
        config.ENFORCE_GATE_FAIL_CLOSED = _prev


def test_clean_gate_emits_no_instrumentation():
    result, msgs = _run_with_capture(_DB())
    assert result.passed is True and result.reason == "all_checks_passed"
    assert not any("GATE_FAILOPEN" in m or "GATE_PASSED_ON_SWALLOW" in m for m in msgs), msgs


def test_legit_reject_unaffected_and_no_swallow_log():
    # kill switch genuinely ON (no exception) -> reject, and no swallow instrumentation.
    result, msgs = _run_with_capture(_DB(kill=True))
    assert result.passed is False and result.reason == "kill_switch_active"
    assert not any("GATE_FAILOPEN" in m or "GATE_PASSED_ON_SWALLOW" in m for m in msgs), msgs


def test_gate_lines_carry_strategy_and_cycle_ts():
    # (B0.5 traceability) a swallow-tagged line carries strategy + cycle_ts so it ties to its
    # candidate/cycle and can correlate forward to a trade after booking (no trade_id pre-booking).
    # Traceability holds regardless of the fail-closed flag (asserts the LOG lines, not the result).
    _, msgs = _run_with_capture(_DB(raise_on={"kill"}),
                                strategy="RAID-C1", cycle_ts="2026-07-07T00:00:00Z")
    assert any("GATE_FAILOPEN" in m and "strategy=RAID-C1" in m and "cycle_ts=2026-07-07T00:00:00Z" in m
               for m in msgs), msgs
    assert any("GATE_PASSED_ON_SWALLOW" in m and "strategy=RAID-C1" in m
               and "cycle_ts=2026-07-07T00:00:00Z" in m for m in msgs), msgs
