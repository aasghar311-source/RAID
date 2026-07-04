"""Consecutive-loss circuit breaker — pure decision helpers (Phase-3).

The runner SETS an auto-pause when the loss streak hits the existing threshold; the worker's
periodic loop CLEARS it once the burst is broken. Both share these tested rules so neither
path can override a manual pause or a kill switch, and the resume side fails CLOSED.
"""

from __future__ import annotations

import config


def should_auto_pause(consecutive_losses: int, threshold: int, already_paused: bool,
                      minutes_since_last_loss, active_window_minutes: int) -> bool:
    """True if the runner should auto-pause NEW entries this cycle.

    Fires only when the streak is ACTIVE — the most recent loss is within active_window_minutes.
    A STALE streak (3+ old losses with nothing recent) must NOT pause: otherwise it re-pauses
    every cycle while auto-resume clears it 15s later, flip-flopping and starving the bot. The
    window matches the resume cooldown so pause (age < window) and resume (age >= window) are
    mutually exclusive. Fails safe: unknown loss age -> do not pause."""
    if already_paused or consecutive_losses < threshold:
        return False
    if minutes_since_last_loss is None:
        return False
    return minutes_since_last_loss < active_window_minutes


def should_auto_resume(pause_entries: bool, operator_note, kill_switch: bool,
                       minutes_since_last_loss, cooldown_minutes: int) -> bool:
    """True if an AUTO consecutive-loss pause may be cleared. Never clears a manual pause
    (an operator_note without the sentinel prefix), never overrides an active kill switch,
    and fails CLOSED when the time since the last loss is unknown (stays paused)."""
    if not pause_entries or kill_switch:
        return False
    if not str(operator_note or "").startswith(config.AUTO_PAUSE_NOTE_PREFIX):
        return False   # manual pause (or no marker) -> never auto-resume
    if minutes_since_last_loss is None:
        return False   # fail closed: unknown streak state -> remain paused
    return minutes_since_last_loss >= cooldown_minutes
