-- Migration 005 — drawdown_state (persist the drawdown high-water mark + ladder/pause state)
-- =========================================================================================
-- Fixes the redeploy-clears-pause bug: runner._peak_equity was an in-memory module global that
-- re-seeded to max(STARTING_EQUITY, equity) on every worker restart, so a redeploy could clear a
-- drawdown pause. This single-row table (id=1) persists the true peak so it survives restarts.
-- Additive, idempotent, operator-run. NEW TABLE ONLY (no ALTER of any existing object). Read and
-- written by db.get_drawdown_state / db.upsert_drawdown_state, gated by config.PERSIST_DRAWDOWN_STATE.

create table if not exists drawdown_state (
    id              integer primary key,
    peak_equity     double precision not null default 0,
    drawdown_pct    double precision not null default 0,
    risk_multiplier double precision not null default 1.0,
    leverage_limit  integer,
    pause_state     text             not null default 'none',   -- none | reduced | paused | shutdown
    updated_at      timestamptz      not null default now(),
    constraint drawdown_state_singleton check (id = 1)
);

-- Seed the single row so the first upsert has a target.
insert into drawdown_state (id) values (1) on conflict (id) do nothing;

-- ROLLBACK (operator-run):
--   drop table if exists drawdown_state;
