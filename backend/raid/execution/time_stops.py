"""Pure exit-decision predicates for the exit path (executor.monitor_positions).

Kept as pure functions so the REAL production exit decisions are unit-testable without
driving the whole async monitor loop:
  * c10_time_stop_due   — RAID-C10 fast 90m time stop (tag-scoped; never catches C1-C5).
  * no_progress_exit_due — cut a stalled trade (not green enough by a time check).
  * classify_stop_reason — label a stop hit as trailing_stop vs stop_loss from sl vs entry.
"""

from __future__ import annotations

from datetime import datetime, timezone

C10_MAX_HOLD_MINUTES = 90


def _parse_iso(ts) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def c10_time_stop_due(reasoning, open_time, now: datetime | None = None,
                      max_minutes: int = C10_MAX_HOLD_MINUTES) -> bool:
    """True iff a trade is a RAID-C10 position older than max_minutes.

    Tag-scoped by an exact 'RAID-C10' prefix so it can never catch RAID-C1 (or any
    other strategy). Fails closed (False) on a missing/malformed tag or open_time.
    """
    if not reasoning or not str(reasoning).startswith("RAID-C10"):
        return False
    opened = _parse_iso(open_time) if open_time else None
    if opened is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - opened).total_seconds() / 60.0 >= max_minutes


def _is_long(direction) -> bool:
    return direction in ("long", "yes")


def no_progress_exit_due(direction, entry, price, hold_minutes: float,
                         check_minutes: float, min_gain_pct: float) -> bool:
    """True iff a trade has been held past `check_minutes` and its CURRENT gain vs entry
    is still below `min_gain_pct` (a fraction, e.g. 0.003 = 0.3%).

    Data (124-trade review): trades not at least +0.3% by 90 min almost never recover and
    drift to a 3h MAT death (~-$2.04 avg); cutting them early (~-$1.00) saves ~$1 each.
    Uses current gain (not peak) so a trade that spiked then gave it all back is also cut.
    Fails closed (False) on missing/bad data or before the time check.
    """
    if hold_minutes < check_minutes:
        return False
    try:
        e = float(entry)
        p = float(price)
    except (TypeError, ValueError):
        return False
    if e <= 0:
        return False
    gain = (p - e) / e if _is_long(direction) else (e - p) / e
    return gain < min_gain_pct


def classify_stop_reason(direction, entry, sl) -> str:
    """Label a stop-hit close as 'trailing_stop' if the stop sits on the PROFITABLE side
    of entry (it was trailed there), else 'stop_loss'.

    Replaces the in-memory `trail_active` flag, which was never persisted so every trailed
    exit was mislabeled 'stop_loss'. If the SL is on the profit side of entry it can only
    have been moved there by the trail. Fails closed to 'stop_loss' on bad data.
    """
    try:
        e = float(entry)
        s = float(sl)
    except (TypeError, ValueError):
        return "stop_loss"
    if e <= 0:
        return "stop_loss"
    trailed = (s > e) if _is_long(direction) else (s < e)
    return "trailing_stop" if trailed else "stop_loss"
