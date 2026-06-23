"""RAID database layer — single async Supabase client + CRUD and schema verification."""

import logging
from datetime import datetime, timezone

from supabase import acreate_client, AsyncClient

import config

log = logging.getLogger("raid.db")

supabase: AsyncClient = None


async def init():
    """Create the async Supabase client; must be awaited once before any DB call."""
    global supabase
    if supabase is None:
        supabase = await acreate_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    return supabase


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


_EXPECTED_TABLES = (
    "trades",
    "equity_snapshots",
    "signals",
    "brain_decisions",
    "daily_stats",
    "kill_switch",
    "learning_adjustments",
    "operator_controls",
    "regime_log",
    "predictions",
    "sizing_state",
    "goal_tracker",
)


async def create_tables():
    """Verify expected tables exist (anon key cannot DDL); warn on any missing."""
    missing = []
    for table in _EXPECTED_TABLES:
        try:
            await supabase.table(table).select("*").limit(1).execute()
        except Exception:  # noqa: BLE001
            missing.append(table)
    if missing:
        log.warning(
            "Missing DB tables: %s. Run schema.sql + Phase 0 SQL in Supabase SQL Editor.",
            ", ".join(missing),
        )
    else:
        log.info("DB schema verified — all %d tables present.", len(_EXPECTED_TABLES))


# ── EQUITY ────────────────────────────────────────────────────────────────

async def get_equity():
    """Return the latest equity snapshot value, or STARTING_EQUITY if none exist.

    NOTE: returns STARTING_EQUITY on BOTH 'no snapshots' and 'read failed'. Callers
    that must not size against a fabricated value (the brain) should use
    get_equity_strict(), which returns None on a read failure. This forgiving
    version is kept for gate.py/executor.py/health which expect a float.
    """
    try:
        res = await (
            supabase.table("equity_snapshots")
            .select("equity")
            .order("timestamp", desc=True)
            .limit(1)
            .execute()
        )
        if res.data:
            return float(res.data[0]["equity"])
    except Exception as exc:  # noqa: BLE001
        log.error("get_equity failed: %s", exc)
    return config.STARTING_EQUITY


async def get_equity_strict():
    """Latest equity as float; STARTING_EQUITY only when the table is genuinely empty.

    Returns None on a READ FAILURE (401/5xx/network) so the brain can abort the
    cycle instead of sizing trades against a fabricated $4000.
    """
    try:
        res = await (
            supabase.table("equity_snapshots")
            .select("equity")
            .order("timestamp", desc=True)
            .limit(1)
            .execute()
        )
        if res.data:
            return float(res.data[0]["equity"])
        return config.STARTING_EQUITY  # genuinely empty table — safe to seed
    except Exception as exc:  # noqa: BLE001
        log.error("get_equity_strict read failure (returning None): %s", exc)
        return None


async def update_equity(equity: float, daily_pnl: float):
    """Insert a new equity snapshot row."""
    try:
        await (
            supabase.table("equity_snapshots")
            .insert({"equity": equity, "daily_pnl": daily_pnl, "paper_mode": config.PAPER_MODE})
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.error("update_equity failed: %s", exc)


async def get_equity_history(days: int = 30):
    """Return daily equity snapshots for the last N days, ordered oldest-first."""
    try:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        res = await (
            supabase.table("equity_snapshots")
            .select("equity, timestamp")
            .gte("timestamp", cutoff)
            .order("timestamp", desc=False)
            .execute()
        )
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        log.error("get_equity_history failed: %s", exc)
        return []


# ── TRADES ────────────────────────────────────────────────────────────────

async def log_trade(trade: dict):
    """Insert a trade row and return its id (empty string on failure)."""
    try:
        res = await supabase.table("trades").insert(trade).execute()
        if res.data:
            return res.data[0]["id"]
    except Exception as exc:  # noqa: BLE001
        log.error("log_trade failed: %s", exc)
    return ""


async def close_trade(trade_id: str, exit_price: float, pnl: float, reason: str):
    """Mark a trade closed with exit price, realized pnl, close time, and reason."""
    try:
        await (
            supabase.table("trades")
            .update({
                "status": "closed",
                "exit_price": exit_price,
                "pnl": pnl,
                "close_time": _now_iso(),
                "close_reason": reason,
            })
            .eq("id", trade_id)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.error("close_trade failed: %s", exc)


async def update_trade_fields(trade_id: str, fields: dict):
    """Update additional fields on a trade (new brain columns)."""
    try:
        await supabase.table("trades").update(fields).eq("id", trade_id).execute()
    except Exception as exc:  # noqa: BLE001
        log.error("update_trade_fields failed: %s", exc)


async def get_open_trades():
    """Return all trades whose status is 'open'."""
    try:
        res = await supabase.table("trades").select("*").eq("status", "open").execute()
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        log.error("get_open_trades failed: %s", exc)
        return []


async def get_open_trades_by_market(market: str):
    """Return open trades filtered by market."""
    try:
        res = await (
            supabase.table("trades")
            .select("*")
            .eq("status", "open")
            .eq("market", market)
            .execute()
        )
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        log.error("get_open_trades_by_market failed: %s", exc)
        return []


async def get_closed_trades_last_n(n: int):
    """Return the N most recently closed trades."""
    try:
        res = await (
            supabase.table("trades")
            .select("*")
            .eq("status", "closed")
            .order("close_time", desc=True)
            .limit(n)
            .execute()
        )
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        log.error("get_closed_trades_last_n failed: %s", exc)
        return []


async def get_consecutive_losses():
    """Return the count of consecutive losing trades from the most recent close."""
    try:
        res = await (
            supabase.table("trades")
            .select("pnl")
            .eq("status", "closed")
            .order("close_time", desc=True)
            .limit(config.CONSECUTIVE_LOSS_LOOKBACK)
            .execute()
        )
        count = 0
        for row in res.data or []:
            if (row.get("pnl") or 0) < 0:
                count += 1
            else:
                break
        return count
    except Exception as exc:  # noqa: BLE001
        log.error("get_consecutive_losses failed: %s", exc)
        return 0


async def get_last_loss_time():
    """Return the close_time (UTC datetime) of the most recent losing trade, or None."""
    try:
        res = await (
            supabase.table("trades")
            .select("close_time")
            .eq("status", "closed")
            .lt("pnl", 0)
            .order("close_time", desc=True)
            .limit(1)
            .execute()
        )
        if res.data and res.data[0].get("close_time"):
            return datetime.fromisoformat(res.data[0]["close_time"].replace("Z", "+00:00"))
    except Exception as exc:  # noqa: BLE001
        log.error("get_last_loss_time failed: %s", exc)
    return None


async def get_trades_for_learning(days: int):
    """Return all closed trades within the last N days (kept for worker compat)."""
    try:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        res = await (
            supabase.table("trades")
            .select("*")
            .eq("status", "closed")
            .gte("close_time", cutoff)
            .execute()
        )
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        log.error("get_trades_for_learning failed: %s", exc)
        return []


# ── SIGNALS ───────────────────────────────────────────────────────────────

async def log_signal(signal: dict):
    try:
        res = await supabase.table("signals").insert(signal).execute()
        if res.data:
            return res.data[0]["id"]
    except Exception as exc:  # noqa: BLE001
        log.error("log_signal failed: %s", exc)
    return ""


async def update_signal(signal_id: str, updates: dict):
    try:
        await supabase.table("signals").update(updates).eq("id", signal_id).execute()
    except Exception as exc:  # noqa: BLE001
        log.error("update_signal failed: %s", exc)


# ── BRAIN DECISIONS ───────────────────────────────────────────────────────

async def log_brain_decision(decision: dict):
    try:
        await supabase.table("brain_decisions").insert(decision).execute()
    except Exception as exc:  # noqa: BLE001
        log.error("log_brain_decision failed: %s", exc)


# ── DAILY STATS ───────────────────────────────────────────────────────────

async def get_daily_stats(date: str):
    try:
        res = await (
            supabase.table("daily_stats").select("*").eq("date", date).limit(1).execute()
        )
        if res.data:
            return res.data[0]
    except Exception as exc:  # noqa: BLE001
        log.error("get_daily_stats failed: %s", exc)
    return {}


async def upsert_daily_stats(stats: dict):
    try:
        await supabase.table("daily_stats").upsert(stats, on_conflict="date").execute()
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_daily_stats failed: %s", exc)


# ── KILL SWITCH ───────────────────────────────────────────────────────────

async def get_kill_switch():
    """Return current kill switch active status (latest record wins)."""
    try:
        res = await (
            supabase.table("kill_switch")
            .select("active")
            .order("activated_at", desc=True)
            .limit(1)
            .execute()
        )
        if res.data:
            return bool(res.data[0]["active"])
    except Exception as exc:  # noqa: BLE001
        log.error("get_kill_switch failed: %s", exc)
    return config.KILL_SWITCH_ACTIVE


async def get_kill_switch_record():
    try:
        res = await (
            supabase.table("kill_switch")
            .select("*")
            .order("activated_at", desc=True)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0]
    except Exception as exc:  # noqa: BLE001
        log.error("get_kill_switch_record failed: %s", exc)
    return {}


async def set_kill_switch(active: bool, reason: str, activated_by: str):
    try:
        await (
            supabase.table("kill_switch")
            .insert({
                "active": active,
                "reason": reason,
                "activated_at": _now_iso(),
                "activated_by": activated_by,
            })
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.error("set_kill_switch failed: %s", exc)


# ── OPERATOR CONTROLS ─────────────────────────────────────────────────────

async def get_operator_controls():
    """Read the single operator_controls row. Returns dict with defaults on failure."""
    defaults = {
        "kill_switch": False,
        "pause_entries": False,
        "emergency_close": False,
        "max_open_trades": config.MAX_OPEN_TRADES,
        "max_position_pct": config.MAX_TRADE_SIZE_PCT,
        "brain_cycle_minutes": config.BRAIN_CYCLE_MINUTES,
        "crypto_enabled": True,
        "stocks_enabled": False,
        "kalshi_enabled": False,
        "options_enabled": False,
        "daily_loss_limit_pct": config.DAILY_LOSS_LIMIT_PCT,
        "alert_on_loss_pct": 0.05,
        "operator_note": None,
    }
    try:
        res = await (
            supabase.table("operator_controls")
            .select("*")
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        if res.data:
            row = res.data[0]
            defaults.update({k: v for k, v in row.items() if v is not None})
            return defaults
    except Exception as exc:  # noqa: BLE001
        log.error("get_operator_controls failed: %s", exc)
    return defaults


async def update_operator_controls(updates: dict) -> bool:
    """Update the operator_controls row. Returns True only if a row was updated.

    Returns False on any failure (no row found, network/DDL error) so callers can
    verify a critical write (e.g. clearing emergency_close) actually persisted.
    """
    try:
        updates["updated_at"] = _now_iso()
        res = await (
            supabase.table("operator_controls")
            .select("id")
            .limit(1)
            .execute()
        )
        if not res.data:
            log.error("update_operator_controls: no operator_controls row to update")
            return False
        row_id = res.data[0]["id"]
        upd = await supabase.table("operator_controls").update(updates).eq("id", row_id).execute()
        return bool(upd.data)
    except Exception as exc:  # noqa: BLE001
        log.error("update_operator_controls failed: %s", exc)
        return False


# ── REGIME LOG ────────────────────────────────────────────────────────────

async def log_regime(entry: dict):
    """Insert a regime_log row."""
    try:
        await supabase.table("regime_log").insert(entry).execute()
    except Exception as exc:  # noqa: BLE001
        log.error("log_regime failed: %s", exc)


# ── PREDICTIONS ───────────────────────────────────────────────────────────

async def log_prediction(entry: dict):
    """Insert a predictions row for calibration tracking."""
    try:
        await supabase.table("predictions").insert(entry).execute()
    except Exception as exc:  # noqa: BLE001
        log.error("log_prediction failed: %s", exc)


# ── SIZING STATE ──────────────────────────────────────────────────────────

async def get_sizing_state():
    """Return the latest sizing_state row as a dict."""
    try:
        res = await (
            supabase.table("sizing_state")
            .select("*")
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0]
    except Exception as exc:  # noqa: BLE001
        log.error("get_sizing_state failed: %s", exc)
    return {
        "kelly_fraction": config.KELLY_FRACTION_DEFAULT,
        "win_rate": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "worst_loss": 0.0,
        "total_trades": 0,
        "sizing_mode": "fractional_kelly",
        "optimal_f": None,
        "trajectory": "ON_TRACK",
    }


async def update_sizing_state(updates: dict):
    """Update the sizing_state row (upsert via the single existing row)."""
    try:
        updates["updated_at"] = _now_iso()
        res = await (
            supabase.table("sizing_state")
            .select("id")
            .limit(1)
            .execute()
        )
        if res.data:
            row_id = res.data[0]["id"]
            await supabase.table("sizing_state").update(updates).eq("id", row_id).execute()
        else:
            await supabase.table("sizing_state").insert(updates).execute()
    except Exception as exc:  # noqa: BLE001
        log.error("update_sizing_state failed: %s", exc)


# ── GOAL TRACKER ──────────────────────────────────────────────────────────

async def log_goal_tracker(entry: dict):
    """Insert a goal_tracker row (logged every brain cycle)."""
    try:
        await supabase.table("goal_tracker").insert(entry).execute()
    except Exception as exc:  # noqa: BLE001
        log.error("log_goal_tracker failed: %s", exc)


# ── LEARNING ADJUSTMENTS (legacy — kept for worker compat) ────────────────

async def log_learning_adjustment(adj: dict):
    try:
        await supabase.table("learning_adjustments").insert(adj).execute()
    except Exception as exc:  # noqa: BLE001
        log.error("log_learning_adjustment failed: %s", exc)


# ── DAILY SPEND (survives Railway restarts) ───────────────────────────────

async def get_spend_today() -> float:
    """Return today's persisted API spend from daily_stats.api_spend_usd, or 0.0."""
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        res = await (
            supabase.table("daily_stats")
            .select("api_spend_usd")
            .eq("date", today)
            .limit(1)
            .execute()
        )
        if res.data and res.data[0].get("api_spend_usd") is not None:
            return float(res.data[0]["api_spend_usd"])
    except Exception as exc:  # noqa: BLE001
        log.error("get_spend_today failed: %s", exc)
    return 0.0


async def persist_spend_today(spend: float) -> None:
    """Merge-safe write of today's API spend to daily_stats.api_spend_usd only."""
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        existing = await (
            supabase.table("daily_stats").select("date").eq("date", today).limit(1).execute()
        )
        if existing.data:
            await (
                supabase.table("daily_stats")
                .update({"api_spend_usd": round(spend, 4)})
                .eq("date", today)
                .execute()
            )
        else:
            await (
                supabase.table("daily_stats")
                .insert({"date": today, "api_spend_usd": round(spend, 4)})
                .execute()
            )
    except Exception as exc:  # noqa: BLE001
        log.error("persist_spend_today failed: %s", exc)


async def get_status_snapshot() -> dict:
    """One-call status for the /stats endpoint."""
    snap = {
        "closed": 0, "open": 0, "closed_with_reasoning": 0,
        "win_rate_pct": 0.0, "realized_pnl": 0.0, "equity": 0.0,
        "api_spend_today": 0.0, "phase2_gate_cleared": False,
    }
    try:
        closed = await supabase.table("trades").select(
            "pnl, claude_reasoning", count="exact"
        ).eq("status", "closed").execute()
        rows = closed.data or []
        snap["closed"] = closed.count if closed.count is not None else len(rows)
        wins = sum(1 for r in rows if (r.get("pnl") or 0) > 0)
        snap["realized_pnl"] = round(sum(float(r.get("pnl") or 0) for r in rows), 2)
        if rows:
            snap["win_rate_pct"] = round(100.0 * wins / len(rows), 1)
        snap["closed_with_reasoning"] = sum(1 for r in rows if r.get("claude_reasoning"))
        snap["phase2_gate_cleared"] = snap["closed_with_reasoning"] >= 10

        open_res = await supabase.table("trades").select(
            "id", count="exact"
        ).eq("status", "open").execute()
        snap["open"] = open_res.count if open_res.count is not None else len(open_res.data or [])

        snap["equity"] = await get_equity()
        snap["api_spend_today"] = await get_spend_today()
    except Exception as exc:  # noqa: BLE001
        log.error("get_status_snapshot failed: %s", exc)
    return snap
