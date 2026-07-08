"""RAID database layer — single async Supabase client + CRUD and schema verification."""

import logging
import os
import re
from datetime import datetime, timedelta, timezone

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


_PAGE_SIZE = 1000  # PostgREST returns at most 1000 rows per response by default


async def _fetch_all(table: str, columns: str, eq_filters=None, page_size: int = _PAGE_SIZE) -> list:
    """Fetch ALL rows from `table` matching the given .eq() filters, paginating past
    PostgREST's default 1000-row response cap via .range(). Without this, aggregations
    over large/growing tables (trades, regime_log, pending_signals) silently truncate
    at 1000 rows and produce wrong sums and counts."""
    rows: list = []
    start = 0
    while True:
        q = supabase.table(table).select(columns)
        for col, val in (eq_filters or []):
            q = q.eq(col, val)
        res = await q.range(start, start + page_size - 1).execute()
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return rows


async def try_claim_lease(row_id: int, worker_id: str, now_iso: str, new_expiry_iso: str) -> bool:
    """Atomic compare-and-set claim of the single worker-lease row. Returns True iff
    THIS worker now holds it (row-level atomic UPDATE). Fail-OPEN (True) if the
    worker_leases table is absent — the single-worker paper default; a missing lock
    must never halt paper trading."""
    try:
        res = await (
            supabase.table("worker_leases")
            .update({"holder_id": worker_id, "expires_at": new_expiry_iso, "updated_at": now_iso})
            .eq("id", row_id)
            .or_(f"holder_id.is.null,expires_at.lt.{now_iso},holder_id.eq.{worker_id}")
            .execute()
        )
        return bool(res.data)
    except Exception as exc:  # noqa: BLE001
        log.warning("try_claim_lease unavailable (%s) — single-worker fail-open", exc)
        return True


async def release_lease(worker_id: str, row_id: int = 1) -> bool:
    """Release the worker lease on graceful shutdown IF this worker holds it, so a redeploy's new
    worker ACQUIRES immediately (no PASSIVE gap). Scoped by holder_id so it never frees ANOTHER
    worker's lease. Best-effort; never raises."""
    try:
        res = await (
            supabase.table("worker_leases")
            .update({"holder_id": None, "expires_at": _now_iso()})
            .eq("id", row_id)
            .eq("holder_id", worker_id)
            .execute()
        )
        return bool(res.data)
    except Exception as exc:  # noqa: BLE001
        log.warning("release_lease failed (%s)", exc)
        return False


async def get_lease(row_id: int = 1):
    """Return the current lease row dict, or None if unavailable."""
    try:
        res = await supabase.table("worker_leases").select("*").eq("id", row_id).limit(1).execute()
        return res.data[0] if res.data else None
    except Exception:  # noqa: BLE001
        return None


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
    "pending_signals",
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
            # NOTE: no paper_mode — equity_snapshots has no such column (never did; the write
            # has failed with PGRST204 since the initial deploy, leaving the table empty). The
            # bot is paper-permanent so the flag is meaningless on a value snapshot. If a live
            # path is ever added, add the column via a migration instead of re-adding it here.
            .insert({"equity": equity, "daily_pnl": daily_pnl})
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.error("update_equity failed: %s", exc)


async def get_realized_equity() -> float:
    """TRUE current equity = STARTING_EQUITY + all realized (net-of-fee) closed-trade P&L. This
    actually compounds — unlike get_equity()/equity_snapshots, which was only written daily and
    left the sizing/ladder pinned at $4000. Used live for the drawdown ladder + deployment cap."""
    return config.STARTING_EQUITY + await get_total_realized_pnl()


# Daily sizing base — recalculated ONCE PER UTC DAY (module cache), so position size compounds
# day-over-day, smoother than a per-trade equity read.
_daily_equity_cache = {"date": None, "equity": None}


async def get_daily_equity_base() -> float:
    """5%-margin sizing base, refreshed once per UTC day. First call of a new day snapshots the
    current realized equity into equity_snapshots (history) and caches it; later calls that day
    return the cached value. On a mid-day worker restart it recomputes once (not per trade)."""
    today = datetime.now(timezone.utc).date().isoformat()
    if _daily_equity_cache["date"] == today and _daily_equity_cache["equity"] is not None:
        return _daily_equity_cache["equity"]
    eq = await get_realized_equity()
    _daily_equity_cache["date"] = today
    _daily_equity_cache["equity"] = eq
    await update_equity(eq, 0.0)   # best-effort daily history row; sizing uses the cache regardless
    return eq


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


# ── B7: signal-quality measurement (measure-only; per-strategy/regime/direction accuracy + R) ──
_STRATEGY_TAG_RE = re.compile(r"^(RAID-C\d+)")
_signal_outcomes_ok = True


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_strategy_tag(reasoning):
    if not reasoning:
        return None
    m = _STRATEGY_TAG_RE.match(str(reasoning))
    return m.group(1) if m else None


def build_signal_outcome_row(trade, trade_id, exit_price, pnl, reason, hold_minutes):
    """B7 measure-only: build a signal_outcomes row from a closing trade. PURE; no I/O. Records the
    generating strategy, direction, the classifier's regime-at-entry AND a completed-bar direction
    reference (the stored 5m entry-slope sign) + the realized ground-truth move, net-of-cost R,
    MFE/MAE, hold time, and whether the direction was correct. Missing inputs -> that field is None
    (never fabricated). Feeds NO decision — record only."""
    t = trade or {}
    direction = t.get("direction")
    long_like = direction in ("long", "yes")
    entry = _to_float(t.get("entry_price"))
    exit_p = _to_float(exit_price)
    size = _to_float(t.get("size_usd"))
    stop_dist = _to_float(t.get("initial_stop_distance_pct"))
    realized_dir = dir_correct = None
    if entry is not None and exit_p is not None and entry > 0:
        realized_dir = "up" if exit_p > entry else "down" if exit_p < entry else "flat"
        dir_correct = (exit_p > entry) if long_like else (exit_p < entry)
    _pnl = _to_float(pnl)
    realized_r = None
    if size and stop_dist and size > 0 and stop_dist > 0 and _pnl is not None:
        realized_r = round(_pnl / (size * stop_dist), 4)   # net-of-cost R (pnl is already net)
    slope = _to_float(t.get("entry_slope"))
    slope_dir = None if slope is None else ("up" if slope > 0 else "down" if slope < 0 else "flat")
    return {
        "trade_id": trade_id,
        "strategy_id": _parse_strategy_tag(t.get("claude_reasoning")),
        "direction": direction,
        "regime_at_entry": t.get("market_regime"),
        "entry_slope": slope,
        "entry_slope_direction": slope_dir,
        "realized_price_direction": realized_dir,
        "direction_correct": dir_correct,
        "entry_price": entry,
        "exit_price": exit_p,
        "realized_r": realized_r,
        "net_pnl": (round(_pnl, 6) if _pnl is not None else None),
        "mfe_pct": _to_float(t.get("peak_pnl_pct")),
        "mae_pct": _to_float(t.get("mae_pct")),
        "hold_minutes": hold_minutes,
        "close_reason": reason,
    }


async def _record_signal_outcome(trade, trade_id, exit_price, pnl, reason, hold_minutes):
    """B7 measure-only: emit a greppable SIGNAL_OUTCOME log (ALWAYS) and persist to signal_outcomes
    (best-effort, self-disabling if absent). Never raises into the close path; feeds no decision."""
    global _signal_outcomes_ok
    try:
        row = build_signal_outcome_row(trade, trade_id, exit_price, pnl, reason, hold_minutes)
    except Exception as exc:  # noqa: BLE001
        log.error("signal_outcome build failed (%s)", exc)
        return
    log.info(
        "SIGNAL_OUTCOME trade=%s strat=%s dir=%s regime=%s slope_dir=%s realized_dir=%s correct=%s "
        "R=%s net_pnl=%s mfe=%s mae=%s hold_min=%s reason=%s",
        row["trade_id"], row["strategy_id"], row["direction"], row["regime_at_entry"],
        row["entry_slope_direction"], row["realized_price_direction"], row["direction_correct"],
        row["realized_r"], row["net_pnl"], row["mfe_pct"], row["mae_pct"], row["hold_minutes"],
        row["close_reason"],
    )
    if not _signal_outcomes_ok:
        return
    try:
        await supabase.table("signal_outcomes").insert(row).execute()
    except Exception as exc:  # noqa: BLE001 — measurement must never affect the close
        msg = str(exc).lower()
        if "pgrst205" in msg or "could not find the table" in msg or "does not exist" in msg:
            _signal_outcomes_ok = False
            log.error("signal_outcomes: table absent — DB recording DISABLED for this process "
                      "(apply migration 008); SIGNAL_OUTCOME logs still emit: %s", exc)
        else:
            log.error("signal_outcome insert failed (%s)", exc)


async def close_trade(trade_id: str, exit_price: float, pnl: float, reason: str, extra: dict | None = None):
    """Mark a trade closed with exit price, realized pnl, close time, and reason.

    Also records hold_minutes centrally (best-effort — a failed read never blocks the close),
    so every close path gets it without touching the 11 call sites. `extra` merges optional
    instrumentation fields (e.g. regime_at_exit) in the SAME atomic update."""
    fields = {
        "status": "closed",
        "exit_price": exit_price,
        "pnl": pnl,
        "close_time": _now_iso(),
        "close_reason": reason,
    }
    _trow: dict = {}
    try:
        # Also fetch the entry-side columns B7 needs (single query — no extra round trip).
        _row = await supabase.table("trades").select(
            "open_time, entry_price, direction, size_usd, symbol, market_regime, claude_reasoning, "
            "initial_stop_distance_pct, peak_pnl_pct, mae_pct, entry_slope"
        ).eq("id", trade_id).limit(1).execute()
        _trow = (_row.data[0] if _row and _row.data else {})
        _ot = _trow.get("open_time")
        if _ot:
            _o = datetime.fromisoformat(str(_ot).replace("Z", "+00:00"))
            if _o.tzinfo is None:
                _o = _o.replace(tzinfo=timezone.utc)
            fields["hold_minutes"] = round((datetime.now(timezone.utc) - _o).total_seconds() / 60.0, 2)
    except Exception:  # noqa: BLE001 — instrumentation must never block a close
        pass
    if extra:
        fields.update(extra)
    _closed_ok = False
    try:
        await supabase.table("trades").update(fields).eq("id", trade_id).execute()
        _closed_ok = True
    except Exception as exc:  # noqa: BLE001
        log.error("close_trade failed: %s", exc)
    # B7 measure-only: record the signal outcome (accuracy + R ledger) on a SUCCESSFUL close.
    # Best-effort; never affects the close; self-disables if signal_outcomes is absent.
    if _closed_ok:
        try:
            await _record_signal_outcome(_trow, trade_id, exit_price, pnl, reason, fields.get("hold_minutes"))
        except Exception as exc:  # noqa: BLE001 — measurement must never affect the close
            log.error("signal_outcome hook failed (%s)", exc)


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


async def get_open_trades_by_symbol(symbol: str):
    """Return open trades filtered by symbol (status='open' = close_time still null)."""
    try:
        res = await (
            supabase.table("trades")
            .select("*")
            .eq("status", "open")
            .eq("symbol", symbol)
            .execute()
        )
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        log.error("get_open_trades_by_symbol failed: %s", exc)
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


async def cleanup_regime_log(hours: int = 48) -> int:
    """Delete regime_log rows older than `hours`. Returns the number deleted (0 on failure
    or if anon-key RLS blocks the delete — never raises). At 5-min cycles regime_log grows
    ~7k rows/day, so this keeps it bounded (~14k rows at 48h retention)."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        res = await supabase.table("regime_log").delete().lt("detected_at", cutoff).execute()
        return len(res.data or [])
    except Exception as exc:  # noqa: BLE001
        log.error("cleanup_regime_log failed: %s", exc)
        return 0


# ── OHLCV CAPTURE (Option B — write-only backtest instrumentation) ──────────
# Persists the raw 5m candles the scanner already fetched this cycle so future regime /
# SL-TP / exit changes can be replayed. This is OBSERVABILITY: no trading code reads it.
#
# FAIL-CLOSED: OFF until the operator sets OHLCV_CAPTURE_ENABLED=true (after migration 004
#   is applied + code deployed). Deploying before the migration writes nothing.
# FAIL-OPEN on the capture path only: every write is wrapped so a missing table / transient
#   fault can NEVER block, delay, or crash a trade cycle — it logs and the cycle continues.
OHLCV_CAPTURE_ENABLED = os.getenv("OHLCV_CAPTURE_ENABLED", "false").lower() in ("1", "true", "yes")
# Capture the last N bars each cycle: the just-closed bar + the still-forming bar. The
# (symbol, bar_ts) upsert makes the forming bar converge to its CLOSED values across cycles.
OHLCV_CAPTURE_TAIL_BARS = 2
_ohlcv_capture_ok = True   # flips False if the table is absent -> silent no-op thereafter


def build_ohlcv_capture_rows(symbol: str, ohlcv: list, tail: int = OHLCV_CAPTURE_TAIL_BARS) -> list:
    """PURE: turn the last `tail` raw 5m candles ([ts,o,h,l,c,v]) into ohlcv_5m upsert rows.
    No I/O, never raises for well-formed input (caller wraps regardless). A row missing the
    OHLC fields is skipped, not fabricated."""
    rows: list = []
    for bar in (ohlcv or [])[-tail:]:
        if not bar or len(bar) < 5:
            continue
        try:
            rows.append({
                "symbol": symbol,
                "bar_ts": datetime.fromtimestamp(int(float(bar[0])), tz=timezone.utc).isoformat(),
                "open": float(bar[1]), "high": float(bar[2]), "low": float(bar[3]),
                "close": float(bar[4]), "volume": float(bar[5]) if len(bar) > 5 else None,
            })
        except (TypeError, ValueError):
            continue   # skip a malformed candle; never fabricate
    return rows


async def capture_ohlcv_5m(rows: list) -> int:
    """Best-effort batched UPSERT into ohlcv_5m (write-only). NEVER raises. Returns the row
    count sent (0 if disabled/empty/failed). Self-disables on a table-absent error (PGRST205)
    so a premature deploy quietly no-ops; a transient error just drops this cycle's rows."""
    global _ohlcv_capture_ok
    if not OHLCV_CAPTURE_ENABLED or not _ohlcv_capture_ok or not rows:
        return 0
    try:
        await supabase.table("ohlcv_5m").upsert(rows, on_conflict="symbol,bar_ts").execute()
        return len(rows)
    except Exception as exc:  # noqa: BLE001 — capture must never propagate into the trade cycle
        msg = str(exc).lower()
        if "pgrst205" in msg or "could not find the table" in msg or "does not exist" in msg:
            _ohlcv_capture_ok = False
            log.error("capture_ohlcv_5m: table 'ohlcv_5m' absent — capture DISABLED for this "
                      "process (apply migration 004): %s", exc)
        else:
            log.error("capture_ohlcv_5m: transient write failure (rows dropped, cycle unaffected): %s", exc)
        return 0


# ── DRAWDOWN STATE (persisted high-water mark; survives restart) ──────────────
# B1: the drawdown ladder's peak-equity high-water mark lives here (single row id=1) so a worker
# restart/redeploy cannot reset it (the in-memory runner._peak_equity re-seeds to 0 on boot).
_drawdown_state_ok = True   # flips False if the table is absent -> in-memory fallback thereafter


async def get_drawdown_state():
    """Return the persisted drawdown_state row (id=1), or None if absent/empty/errored. None =>
    the caller falls back to the in-memory high-water mark (a missing table must never fabricate a
    drawdown/pause). Self-disables on a table-absent error so a premature deploy quietly no-ops."""
    global _drawdown_state_ok
    if not _drawdown_state_ok:
        return None
    try:
        res = await supabase.table("drawdown_state").select("*").eq("id", 1).limit(1).execute()
        return res.data[0] if res.data else None
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "pgrst205" in msg or "could not find the table" in msg or "does not exist" in msg:
            _drawdown_state_ok = False
            log.error("get_drawdown_state: table 'drawdown_state' absent — persistence DISABLED "
                      "for this process (apply migration 005): %s", exc)
        else:
            log.warning("get_drawdown_state failed (%s) — in-memory fallback this cycle", exc)
        return None


async def upsert_drawdown_state(fields: dict) -> bool:
    """Best-effort UPSERT of the single drawdown_state row (id=1). Returns True on success, False
    otherwise. Never raises into the trade cycle. Self-disables on a table-absent error."""
    global _drawdown_state_ok
    if not _drawdown_state_ok:
        return False
    try:
        payload = dict(fields)
        payload["id"] = 1
        payload["updated_at"] = _now_iso()
        res = await supabase.table("drawdown_state").upsert(payload, on_conflict="id").execute()
        return bool(res.data)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "pgrst205" in msg or "could not find the table" in msg or "does not exist" in msg:
            _drawdown_state_ok = False
            log.error("upsert_drawdown_state: table 'drawdown_state' absent — persistence DISABLED "
                      "for this process (apply migration 005): %s", exc)
        else:
            log.error("upsert_drawdown_state failed (%s)", exc)
        return False


# ── COST ESTIMATES (B4: versioned dynamic cost recorded per trade) ────────────
_cost_estimates_ok = True   # flips False if the table is absent -> recording no-ops thereafter


async def insert_cost_estimate(row: dict) -> bool:
    """Best-effort insert of a versioned cost estimate (keyed by trade_id) into cost_estimates.
    RECORD-ONLY — never feeds the live gate/P&L. Never raises; self-disables on a table-absent
    error so a premature deploy quietly no-ops."""
    global _cost_estimates_ok
    if not _cost_estimates_ok or not row:
        return False
    try:
        res = await supabase.table("cost_estimates").insert(row).execute()
        return bool(res.data)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "pgrst205" in msg or "could not find the table" in msg or "does not exist" in msg:
            _cost_estimates_ok = False
            log.error("insert_cost_estimate: table 'cost_estimates' absent — recording DISABLED "
                      "for this process (apply migration 006): %s", exc)
        else:
            log.error("insert_cost_estimate failed (%s)", exc)
        return False


# ── QUOTE PATHS (B5: open-position quote flight recorder; batched, non-blocking) ──
_quote_paths_ok = True   # flips False if the table is absent -> capture no-ops thereafter


async def persist_quote_paths(rows: list) -> int:
    """Best-effort batched INSERT of quote-path records into position_quote_paths. Called
    fire-and-forget from the exit monitor, so it NEVER raises and returns the row count written
    (0 on disabled/empty/failed). Self-disables on a table-absent error."""
    global _quote_paths_ok
    if not _quote_paths_ok or not rows:
        return 0
    try:
        await supabase.table("position_quote_paths").insert(rows).execute()
        return len(rows)
    except Exception as exc:  # noqa: BLE001 — flight recorder must never affect exits
        msg = str(exc).lower()
        if "pgrst205" in msg or "could not find the table" in msg or "does not exist" in msg:
            _quote_paths_ok = False
            log.error("persist_quote_paths: table 'position_quote_paths' absent — capture DISABLED "
                      "for this process (apply migration 007): %s", exc)
        else:
            log.error("persist_quote_paths: transient write failure (rows dropped): %s", exc)
        return 0


# ── MARKET STATE (Stage-C spine SHADOW; measure-only) ─────────────────────────
_market_state_ok = True


async def persist_market_state(row: dict) -> bool:
    """Best-effort insert of one market-state spine row (SHADOW) into market_state_log. Returns True
    on success. Never raises into the cycle; self-disables on a table-absent error."""
    global _market_state_ok
    if not _market_state_ok or not row:
        return False
    try:
        res = await supabase.table("market_state_log").insert(row).execute()
        return bool(res.data)
    except Exception as exc:  # noqa: BLE001 — shadow spine must never affect the cycle
        msg = str(exc).lower()
        if "pgrst205" in msg or "could not find the table" in msg or "does not exist" in msg:
            _market_state_ok = False
            log.error("market_state_log: table absent — persistence DISABLED for this process "
                      "(apply migration 009): %s", exc)
        else:
            log.error("persist_market_state failed (%s)", exc)
        return False


# ── PAIR LIQUIDITY METRICS (Appendix-C §2 layer, C.6 SHADOW; measure-only) ─────
_pair_liquidity_ok = True


async def persist_pair_liquidity(rows: list) -> int:
    """Best-effort BATCHED insert of §2 pair-liquidity metric rows (C.6 SHADOW) into
    pair_liquidity_metrics. Returns the count written (0 if disabled/empty/failed). Never raises into
    the cycle; self-disables on a table-absent error (apply migration 010)."""
    global _pair_liquidity_ok
    if not _pair_liquidity_ok or not rows:
        return 0
    try:
        res = await supabase.table("pair_liquidity_metrics").insert(rows).execute()
        return len(res.data or [])
    except Exception as exc:  # noqa: BLE001 — shadow metrics must never affect the cycle
        msg = str(exc).lower()
        if "pgrst205" in msg or "could not find the table" in msg or "does not exist" in msg:
            _pair_liquidity_ok = False
            log.error("pair_liquidity_metrics: table absent — persistence DISABLED for this process "
                      "(apply migration 010): %s", exc)
        else:
            log.error("persist_pair_liquidity failed (%s)", exc)
        return 0


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


# ── PENDING SIGNALS ──────────────────────────────────────────────────────────

async def save_pending_signals(signals: list):
    """Insert pending signal rows, marked armed with expiry from config."""
    try:
        now = datetime.now(timezone.utc)
        expiry = now + timedelta(minutes=config.PENDING_SIGNAL_EXPIRY_MIN)
        rows = []
        for s in (signals or []):
            rows.append({
                "symbol": s.get("symbol"),
                "direction": s.get("direction"),
                "tier": s.get("tier", "conviction"),
                "trigger_type": s.get("trigger_type", "limit"),
                "trigger_price": s.get("trigger_price"),
                "stop_loss": s.get("stop_loss"),
                "take_profit": s.get("take_profit"),
                "size_pct": s.get("size_pct"),
                "probability": s.get("probability"),
                "ladder_group": s.get("ladder_group"),
                "reasoning": s.get("reasoning"),
                "regime": s.get("regime"),
                "status": "armed",
                "created_at": now.isoformat(),
                "expires_at": expiry.isoformat(),
            })
        if rows:
            await supabase.table("pending_signals").insert(rows).execute()
            log.info("PENDING: saved %d armed signals (expire %s)", len(rows), expiry.isoformat())
    except Exception as exc:  # noqa: BLE001
        log.error("save_pending_signals failed: %s", exc)


async def cancel_armed_signals():
    """Cancel all currently armed signals (called at start of each brain cycle)."""
    try:
        await (
            supabase.table("pending_signals")
            .update({"status": "cancelled"})
            .eq("status", "armed")
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.error("cancel_armed_signals failed: %s", exc)


async def get_armed_signals():
    """Return all armed pending signals that have not yet expired."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        res = await (
            supabase.table("pending_signals")
            .select("*")
            .eq("status", "armed")
            .gte("expires_at", now)
            .execute()
        )
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        log.error("get_armed_signals failed: %s", exc)
        return []


async def update_signal_status(signal_id: str, status: str):
    """Update a pending signal's status (filled/expired/cancelled)."""
    try:
        await (
            supabase.table("pending_signals")
            .update({"status": status})
            .eq("id", signal_id)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.error("update_signal_status failed for %s: %s", signal_id, exc)


async def expire_armed_signals():
    """Mark armed signals past their expires_at as expired."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        await (
            supabase.table("pending_signals")
            .update({"status": "expired"})
            .eq("status", "armed")
            .lt("expires_at", now)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.error("expire_armed_signals failed: %s", exc)


async def get_recent_signal_outcomes(minutes: int = 35):
    """Return filled and expired signals from the last N minutes for brain feedback."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        res = await (
            supabase.table("pending_signals")
            .select("symbol, direction, tier, trigger_type, trigger_price, probability, status, reasoning")
            .in_("status", ["filled", "expired"])
            .gte("created_at", cutoff)
            .execute()
        )
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        log.error("get_recent_signal_outcomes failed: %s", exc)
        return []


async def get_total_realized_pnl():
    """Sum of pnl across all closed trades = total realized P&L."""
    try:
        rows = await _fetch_all("trades", "pnl", [("status", "closed")])
        return sum(float(r.get("pnl") or 0) for r in rows)
    except Exception as exc:  # noqa: BLE001
        log.error("get_total_realized_pnl failed: %s", exc)
        return 0.0


async def save_latest_news(news_by_symbol: dict):
    """Upsert latest per-symbol news to latest_news so the terminal can display
    a fresh news feed (one row per symbol, overwritten each cycle)."""
    try:
        rows = []
        for sym, info in (news_by_symbol or {}).items():
            if not info or not info.get("headline"):
                continue
            rows.append({
                "symbol": sym,
                "headline": info.get("headline"),
                "sentiment": info.get("sentiment", "neutral"),
                "published_at": info.get("published_at"),
            })
        if rows:
            await supabase.table("latest_news").upsert(rows, on_conflict="symbol").execute()
    except Exception as exc:  # noqa: BLE001
        log.error("save_latest_news failed: %s", exc)


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
        rows = await _fetch_all("trades", "pnl, claude_reasoning", [("status", "closed")])
        snap["closed"] = len(rows)
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
        # Deployment headroom: how much of equity is in open positions vs the cap.
        try:
            _open = await get_open_trades()
            _deployed = sum(float(t.get("size_usd") or 0) for t in _open)
            _eq = snap["equity"] or 0
            snap["deployed_usd"] = round(_deployed, 2)
            snap["deployed_pct"] = round(100.0 * _deployed / _eq, 1) if _eq > 0 else 0.0
            snap["deployment_cap_pct"] = round(config.MAX_EQUITY_DEPLOYED_PCT * 100, 1)
        except Exception as exc:  # noqa: BLE001
            log.error("deployment snapshot failed: %s", exc)
        # Entries-by-probability-bucket: how many trades the brain took at each
        # confidence level (shows impact of the 0.45 MIN_CONFIDENCE floor).
        try:
            _pb_rows = await _fetch_all("trades", "predicted_prob", [("status", "closed")])
            _buckets = {}
            for _r in _pb_rows:
                _p = _r.get("predicted_prob")
                if _p is None:
                    continue
                _k = round(float(_p), 1)
                _buckets[str(_k)] = _buckets.get(str(_k), 0) + 1
            snap["entries_by_prob"] = dict(sorted(_buckets.items()))
        except Exception as exc:  # noqa: BLE001
            log.error("entries_by_prob failed: %s", exc)
            snap["entries_by_prob"] = {}
        snap["api_spend_today"] = await get_spend_today()

        # Calibration readout: for closed trades, bucket by stated probability
        # and compare to actual win rate. Shows whether the brain's confidence
        # is honest (e.g. a "0.70" bucket should win ~70%).
        try:
            cal_rows = await _fetch_all("trades", "predicted_prob, pnl", [("status", "closed")])
            buckets = {}
            for r in cal_rows:
                p = r.get("predicted_prob")
                if p is None:
                    continue
                key = round(float(p), 1)  # 0.5, 0.6, 0.7, 0.8...
                b = buckets.setdefault(key, {"n": 0, "wins": 0})
                b["n"] += 1
                if (r.get("pnl") or 0) > 0:
                    b["wins"] += 1
            snap["calibration"] = {
                str(k): {
                    "stated_pct": int(k * 100),
                    "n": v["n"],
                    "actual_win_pct": round(100.0 * v["wins"] / v["n"], 0) if v["n"] else 0,
                }
                for k, v in sorted(buckets.items())
            }
        except Exception as exc:  # noqa: BLE001
            log.error("calibration readout failed: %s", exc)
            snap["calibration"] = {}
    except Exception as exc:  # noqa: BLE001
        log.error("get_status_snapshot failed: %s", exc)
    return snap
