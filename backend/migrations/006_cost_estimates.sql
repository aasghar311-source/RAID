-- Migration 006 — cost_estimates (versioned dynamic cost estimate per trade)
-- ==========================================================================
-- B4: records the versioned dynamic cost estimate (components + cost_model_version) for each booked
-- trade, keyed by trade_id. Deliberately a NEW TABLE (not an ALTER of `trades`) so cost_model_version
-- does not touch the drifted trades schema before the information_schema dump. Additive, idempotent,
-- operator-run. RECORD-ONLY: the live gate/P&L still charge the flat 1.04% floor.

create table if not exists cost_estimates (
    id                     bigserial primary key,
    trade_id               text,
    pair                   text,
    direction              text,
    cost_model_version     text not null,
    entry_fee_pct          double precision,
    exit_fee_pct           double precision,
    margin_open_pct        double precision,
    spread_pct             double precision,
    slippage_pct           double precision,
    rollover_reserve_pct   double precision,
    uncertainty_buffer_pct double precision,
    floor_pct              double precision,
    total_pct              double precision,
    created_at             timestamptz not null default now()
);
create index if not exists cost_estimates_trade_idx on cost_estimates (trade_id);

-- ROLLBACK (operator-run):
--   drop table if exists cost_estimates;
