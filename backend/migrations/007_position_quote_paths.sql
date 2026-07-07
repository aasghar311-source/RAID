-- Migration 007 — position_quote_paths (open-position quote flight recorder)
-- =========================================================================
-- B5: records per-tick quote evidence for open positions so exit-engine changes can be REPLAYED
-- (the 1s quote path was previously never persisted — only 5m candles survived). Written batched +
-- fire-and-forget from the exit monitor — it never blocks the loop. Additive, idempotent,
-- operator-run. NOTE: high write rate — add a retention policy (e.g. delete rows older than N days)
-- once volume is understood.

create table if not exists position_quote_paths (
    id                   bigserial primary key,
    trade_id             text,
    pair                 text,
    ts                   timestamptz,
    bid                  double precision,
    ask                  double precision,
    mid                  double precision,
    spread               double precision,
    effective_exit_price double precision,
    direction            text,
    mfe                  double precision,
    mae                  double precision,
    source               text,
    freshness_s          double precision,
    quote_validity       boolean,
    created_at           timestamptz not null default now()
);
create index if not exists position_quote_paths_trade_idx on position_quote_paths (trade_id);
create index if not exists position_quote_paths_ts_idx on position_quote_paths (ts);

-- ROLLBACK (operator-run):
--   drop table if exists position_quote_paths;
