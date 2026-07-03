"""Per-strategy time stops for the exit path (executor.monitor_positions).

Pure predicates so the REAL production exit decision is unit-testable without driving
the whole async monitor loop. C10 (liquidity-sweep reversal) positions resolve fast, so
a RAID-C10 trade gets a far tighter cap than the 3h trend max-hold — enforced here,
scoped strictly by the strategy tag so C1-C5 trades are never affected.
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
