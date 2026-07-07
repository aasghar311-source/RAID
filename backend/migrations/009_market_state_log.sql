-- Migration 009 — market_state_log (Stage-C market-state spine, SHADOW measure-only)
-- =================================================================================
-- Persists the layered market-state spine (F1-F5 + BTC/ETH/SOL majors + alt breadth) computed every
-- cycle ALONGSIDE the legacy 5m regime label, so the new spine's lead/lag vs the lagging classifier
-- can be measured. MEASURE-ONLY — the spine books nothing, feeds no decision, changes no sizing/exit.
-- NEW TABLE (not an ALTER). Additive, idempotent, operator-run. Written by db.persist_market_state
-- from raid.runner._market_state_shadow.

create table if not exists market_state_log (
    id                    bigserial primary key,
    cycle_ts              timestamptz,
    portfolio_state       text,               -- F1: RISK_ON/RISK_OFF/MIXED/CRISIS/UNKNOWN
    fast_direction        text,               -- F2: LONG/SHORT/NEUTRAL/UNKNOWN (post veto)
    excursion_veto        boolean,            -- F3
    structure             text,               -- F4: TREND_UP/TREND_DOWN/RANGE/UNKNOWN
    breadth_pct_up        double precision,   -- F5
    breadth_median_return double precision,
    breadth_dispersion    double precision,
    breadth_n             integer,
    reference_symbol      text,               -- market leader used for F2/F3/F4 (BTC else ETH)
    legacy_regime_ref     text,               -- legacy classifier's regime for the SAME reference
    majors_json           text,               -- BTC/ETH/SOL {dir, atr_1h_pct}
    votes_json            text,               -- F2 per-vote breakdown
    created_at            timestamptz not null default now()
);
create index if not exists market_state_log_ts_idx on market_state_log (cycle_ts);

-- ROLLBACK (operator-run):
--   drop table if exists market_state_log;
