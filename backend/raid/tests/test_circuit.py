"""Consecutive-loss circuit-breaker tests (Phase-3 auto-pause / auto-resume).

Verifies: pause fires at the existing threshold; a manual pause (or kill switch) is never
auto-resumed; the auto-pause resumes only after the cooldown elapses with no newer loss; and
the resume fails closed when the streak state is unknown.

Discovered and run by raid.tests.run_all (plain asserts, no pytest).
"""

import config
from raid.core.circuit import should_auto_pause, should_auto_resume

PREFIX = config.AUTO_PAUSE_NOTE_PREFIX
THRESH = config.CONSECUTIVE_LOSS_PAUSE          # 3
COOL = config.CONSEC_LOSS_PAUSE_COOLDOWN_MINUTES  # 30


# ── pause ─────────────────────────────────────────────────────────────────────
def test_pause_fires_at_threshold():
    assert should_auto_pause(THRESH, THRESH, already_paused=False) is True
    assert should_auto_pause(THRESH + 2, THRESH, already_paused=False) is True


def test_pause_not_below_threshold():
    assert should_auto_pause(THRESH - 1, THRESH, already_paused=False) is False


def test_pause_not_when_already_paused():
    # Idempotent: never re-stamp/re-fire when entries are already paused.
    assert should_auto_pause(THRESH + 5, THRESH, already_paused=True) is False


# ── resume ────────────────────────────────────────────────────────────────────
def test_resume_after_cooldown_for_auto_pause():
    note = f"{PREFIX} 3 losses @ 2026-07-03T23:00:00+00:00"
    assert should_auto_resume(True, note, False, COOL, COOL) is True
    assert should_auto_resume(True, note, False, COOL + 5, COOL) is True


def test_no_resume_before_cooldown():
    note = f"{PREFIX} 3 losses"
    assert should_auto_resume(True, note, False, COOL - 1, COOL) is False


def test_never_resume_manual_pause():
    # A manual pause (Settings toggle) has no sentinel prefix -> never auto-cleared.
    assert should_auto_resume(True, None, False, COOL + 100, COOL) is False
    assert should_auto_resume(True, "operator paused for maintenance", False, COOL + 100, COOL) is False


def test_never_override_kill_switch():
    note = f"{PREFIX} 3 losses"
    assert should_auto_resume(True, note, kill_switch=True, minutes_since_last_loss=COOL + 100,
                              cooldown_minutes=COOL) is False


def test_fail_closed_when_streak_unknown():
    # get_last_loss_time() returned None -> stay paused (fail closed).
    note = f"{PREFIX} 3 losses"
    assert should_auto_resume(True, note, False, None, COOL) is False


def test_no_resume_when_not_paused():
    assert should_auto_resume(False, None, False, COOL + 100, COOL) is False
