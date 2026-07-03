"""Tests for the order state machine — legality, terminal safety, UNKNOWN freeze."""

from raid.core.candidate import CandidateStatus as S
from raid.execution.state_machine import OrderStateMachine, IllegalTransition


def test_happy_path_full_lifecycle():
    m = OrderStateMachine("o1")
    seq = [S.VALIDATED, S.ARMED, S.SUBMITTING, S.ACKNOWLEDGED, S.FILLED,
           S.PROTECTION_PENDING, S.PROTECTED, S.EXIT_SUBMITTING, S.EXIT_ACKNOWLEDGED, S.CLOSED]
    for st in seq:
        m.transition(st, "ok")
    assert m.status == S.CLOSED
    assert m.is_terminal
    assert len(m.history) == len(seq)


def test_submission_is_not_a_fill():
    m = OrderStateMachine("o2")
    m.transition(S.VALIDATED)
    m.transition(S.ARMED)
    m.transition(S.SUBMITTING)
    # SUBMITTING -> FILLED is illegal; must be ACKNOWLEDGED first.
    try:
        m.transition(S.FILLED)
        raise AssertionError("submission must not become a fill directly")
    except IllegalTransition:
        pass
    m.transition(S.ACKNOWLEDGED)
    m.transition(S.FILLED)
    assert m.is_filled


def test_illegal_from_candidate():
    m = OrderStateMachine("o3")
    for bad in (S.FILLED, S.PROTECTED, S.CLOSED, S.ARMED):
        try:
            OrderStateMachine("o3b", status=S.CANDIDATE).transition(bad)
            raise AssertionError(f"CANDIDATE -> {bad} should be illegal")
        except IllegalTransition:
            pass


def test_terminal_is_frozen():
    m = OrderStateMachine("o4", status=S.CLOSED)
    try:
        m.transition(S.PROTECTED)
        raise AssertionError("no transition out of a terminal state")
    except IllegalTransition:
        pass


def test_unknown_freezes_and_reconciles():
    m = OrderStateMachine("o5")
    m.transition(S.VALIDATED)
    m.transition(S.ARMED)
    m.transition(S.SUBMITTING)
    m.transition(S.ACKNOWLEDGED)
    m.to_unknown("ack_timeout")
    assert m.status == S.UNKNOWN
    assert m.risk_frozen is True
    # UNKNOWN can only reconcile.
    try:
        m.transition(S.FILLED)
        raise AssertionError("UNKNOWN must reconcile before anything else")
    except IllegalTransition:
        pass
    m.transition(S.RECONCILIATION_REQUIRED)
    assert m.risk_frozen is True
    m.transition(S.CLOSED, "reconciled_flat")
    assert m.is_terminal


def test_to_unknown_from_terminal_raises():
    m = OrderStateMachine("o6", status=S.CANCELED)
    try:
        m.to_unknown("late")
        raise AssertionError("cannot go UNKNOWN from terminal")
    except IllegalTransition:
        pass
