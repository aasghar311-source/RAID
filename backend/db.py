"""RAID database layer — single Supabase client, async-wrapped CRUD + schema bootstrap."""

import asyncio
import logging
from datetime import datetime, timezone

from supabase import Client, create_client

import config

log = logging.getLogger("raid.db")

# Single Supabase client instance created once at module level.
supabase: Client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)

# Full schema. Run this once in the Supabase SQL Editor (it is also committed to
# the repo as schema.sql). The anon key cannot issue DDL, so the bot does NOT
# auto-create tables — create_tables() only verifies they exist on boot.
_SCHEMA_SQL = """
create extension if not exists "pgcrypto";

create table if not exists trades (
    id uuid primary key default gen_random_uuid(),
    bot_name text,
    market text,
    symbol text,
    direction text,
    entry_price float,
    exit_price float,
    size_usd float,
    confidence float,
    pnl float default 0,
    status text default 'open',
    open_time timestamptz default now(),
    close_time timestamptz,
    close_reason text,
    paper_mode boolean default true,
    sl float,
    tp float
);

create table if not exists equity_snapshots (
    id uuid primary key default gen_random_uuid(),
    equity float,
    daily_pnl float default 0,
    timestamp timestamptz default now(),
    paper_mode boolean default true
);

create table if not exists signals (
    id uuid primary key default gen_random_uuid(),
    market text,
    symbol text,
    direction text,
    confidence float,
    news_sentiment text,
    technical_score float,
    ai_validated boolean default false,
    ai_decision text,
    rejected_reason text,
    entered_trade boolean default false,
    timestamp timestamptz default now()
);

create table if not exists brain_decisions (
    id uuid primary key default gen_random_uuid(),
    signal_id uuid,
    prompt_tokens int,
    response_tokens int,
    cost_usd float,
    decision text,
    reasoning text,
    timestamp timestamptz default now()
);

create table if not exists daily_stats (
    id uuid primary key default gen_random_uuid(),
    date date unique,
    total_trades int default 0,
    wins int default 0,
    losses int default 0,
    pnl float default 0,
    win_rate float default 0,
    ai_spend float default 0,
    paper_mode boolean default true
);

create table if not exists kill_switch (
    id uuid primary key default gen_random_uuid(),
    active boolean default false,
    reason text,
    activated_at timestamptz,
    activated_by text
);

create table if not exists learning_adjustments (
    id uuid primary key default gen_random_uuid(),
    market text,
    signal_type text,
    old_weight float,
    new_weight float,
    win_rate float,
    sample_size int,
    applied_at timestamptz default now()
);
"""


def _now_iso():
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


_EXPECTED_TABLES = (
    "trades",
    "equity_snapshots",
    "signals",
    "brain_decisions",
    "daily_stats",
    "kill_switch",
    "learning_adjustments",
)


async def create_tables():
    """Verify the expected tables exist (anon key cannot DDL); warn on any that are missing."""
    missing = []
    for table in _EXPECTED_TABLES:
        try:
            await asyncio.to_thread(
                lambda t=table: supabase.table(t).select("*").limit(1).execute()
            )
        except Exception:  # noqa: BLE001
            missing.append(table)
    if missing:
        log.warning(
            "Missing DB tables: %s. Run schema.sql in the Supabase SQL Editor before trading.",
            ", ".join(missing),
        )
    else:
        log.info("DB schema verified — all %d tables present.", len(_EXPECTED_TABLES))


async def get_equity():
    """Return the latest equity snapshot value, or STARTING_EQUITY if none exist."""
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("equity_snapshots")
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


async def update_equity(equity: float, daily_pnl: float):
    """Insert a new equity snapshot row."""
    try:
        await asyncio.to_thread(
            lambda: supabase.table("equity_snapshots")
            .insert(
                {
                    "equity": equity,
                    "daily_pnl": daily_pnl,
                    "paper_mode": config.PAPER_MODE,
                }
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.error("update_equity failed: %s", exc)


async def log_trade(trade: dict):
    """Insert a trade row and return its id (empty string on failure)."""
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("trades").insert(trade).execute()
        )
        if res.data:
            return res.data[0]["id"]
    except Exception as exc:  # noqa: BLE001
        log.error("log_trade failed: %s", exc)
    return ""


async def close_trade(trade_id: str, exit_price: float, pnl: float, reason: str):
    """Mark a trade closed with its exit price, realized pnl, close time, and reason."""
    try:
        await asyncio.to_thread(
            lambda: supabase.table("trades")
            .update(
                {
                    "status": "closed",
                    "exit_price": exit_price,
                    "pnl": pnl,
                    "close_time": _now_iso(),
                    "close_reason": reason,
                }
            )
            .eq("id", trade_id)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.error("close_trade failed: %s", exc)


async def log_signal(signal: dict):
    """Insert a signal row and return its id (empty string on failure)."""
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("signals").insert(signal).execute()
        )
        if res.data:
            return res.data[0]["id"]
    except Exception as exc:  # noqa: BLE001
        log.error("log_signal failed: %s", exc)
    return ""


async def update_signal(signal_id: str, updates: dict):
    """Update an existing signal record by id."""
    try:
        await asyncio.to_thread(
            lambda: supabase.table("signals").update(updates).eq("id", signal_id).execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.error("update_signal failed: %s", exc)


async def log_brain_decision(decision: dict):
    """Insert a brain_decisions row."""
    try:
        await asyncio.to_thread(
            lambda: supabase.table("brain_decisions").insert(decision).execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.error("log_brain_decision failed: %s", exc)


async def get_open_trades():
    """Return all trades whose status is 'open'."""
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("trades").select("*").eq("status", "open").execute()
        )
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        log.error("get_open_trades failed: %s", exc)
        return []


async def get_open_trades_by_market(market: str):
    """Return open trades filtered by market."""
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("trades")
            .select("*")
            .eq("status", "open")
            .eq("market", market)
            .execute()
        )
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        log.error("get_open_trades_by_market failed: %s", exc)
        return []


async def get_consecutive_losses():
    """Return the count of consecutive losing trades from the most recent close."""
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("trades")
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


async def get_daily_stats(date: str):
    """Return the daily_stats row for the given date as a dict, or {} if absent."""
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("daily_stats").select("*").eq("date", date).limit(1).execute()
        )
        if res.data:
            return res.data[0]
    except Exception as exc:  # noqa: BLE001
        log.error("get_daily_stats failed: %s", exc)
    return {}


async def upsert_daily_stats(stats: dict):
    """Insert or update the daily_stats row for its date (unique key)."""
    try:
        await asyncio.to_thread(
            lambda: supabase.table("daily_stats").upsert(stats, on_conflict="date").execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.error("upsert_daily_stats failed: %s", exc)


async def get_kill_switch():
    """Return the current kill switch active status (latest record wins)."""
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("kill_switch")
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
    """Return the latest kill_switch record (dict) or {} if none exist."""
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("kill_switch")
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
    """Record a new kill switch state (the latest record is the current state)."""
    try:
        await asyncio.to_thread(
            lambda: supabase.table("kill_switch")
            .insert(
                {
                    "active": active,
                    "reason": reason,
                    "activated_at": _now_iso(),
                    "activated_by": activated_by,
                }
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.error("set_kill_switch failed: %s", exc)


async def log_learning_adjustment(adj: dict):
    """Insert a learning_adjustments row."""
    try:
        await asyncio.to_thread(
            lambda: supabase.table("learning_adjustments").insert(adj).execute()
        )
    except Exception as exc:  # noqa: BLE001
        log.error("log_learning_adjustment failed: %s", exc)


async def get_trades_for_learning(days: int):
    """Return all closed trades whose close_time is within the last N days."""
    try:
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        res = await asyncio.to_thread(
            lambda: supabase.table("trades")
            .select("*")
            .eq("status", "closed")
            .gte("close_time", cutoff)
            .execute()
        )
        return res.data or []
    except Exception as exc:  # noqa: BLE001
        log.error("get_trades_for_learning failed: %s", exc)
        return []
