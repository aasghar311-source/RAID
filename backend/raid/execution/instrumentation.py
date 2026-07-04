"""Per-trade excursion instrumentation (Commit 1) — pure helpers, no I/O.

The executor's management loop calls excursion_update() each cycle with the live
unrealized %move; it returns ONLY the changed fields to persist (high-water MFE + its
timing, and the full adverse excursion MAE + its timing). This is measurement only —
it never influences an exit decision, and the exit ladder is untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone


def minutes_since(iso_ts, now: datetime | None = None) -> float:
    """Minutes elapsed since an ISO-8601 timestamp (UTC-safe). Returns 0.0 on a missing or
    unparseable timestamp (fail soft — instrumentation must never break an exit check).
    `now` is injectable for deterministic tests; defaults to the current UTC time."""
    if not iso_ts:
        return 0.0
    try:
        t = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 0.0
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - t).total_seconds() / 60.0)


def excursion_update(prev_peak: float, prev_mae, cur_pct: float, minutes: float) -> dict:
    """Return the dict of trade fields to persist given the current unrealized %move.

    prev_peak is the running high-water (already floored at 0 by the caller, preserving the
    existing peak_pnl_pct semantics); prev_mae is the running trough (None until first set).
    A new high updates peak_pnl_pct + mfe_minutes_from_entry; a new low updates mae_pct +
    mae_minutes_from_entry. Returns {} when nothing changed (no write). Pure."""
    out: dict = {}
    if cur_pct > prev_peak:
        out["peak_pnl_pct"] = round(cur_pct, 3)
        out["mfe_minutes_from_entry"] = round(minutes, 2)
    if prev_mae is None or cur_pct < prev_mae:
        out["mae_pct"] = round(cur_pct, 3)
        out["mae_minutes_from_entry"] = round(minutes, 2)
    return out
