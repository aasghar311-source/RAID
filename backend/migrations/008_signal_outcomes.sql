-- Migration 008 — signal_outcomes (signal-quality measurement ledger, per closed trade)
-- ====================================================================================
-- B7: on every trade close, records the generating strategy, direction, the live classifier's
-- regime-at-entry AND a completed-bar direction reference (entry-slope sign) + the realized
-- ground-truth move, net-of-cost realized R, net P&L (already cost-adjusted), MFE/MAE, hold time,
-- and whether the direction was correct. Starts the per-strategy/regime/direction accuracy +
-- expectancy ledger on the OLD signal so Stage C has a baseline to beat. MEASURE-ONLY — feeds no
-- gate/sizing/exit. NEW TABLE (not an ALTER of `trades`) — keyed by trade_id. Additive, idempotent,
-- operator-run. Written by db._record_signal_outcome from db.close_trade (all close paths).

create table if not exists signal_outcomes (
    id                        bigserial primary key,
    trade_id                  text,
    strategy_id               text,
    direction                 text,
    regime_at_entry           text,               -- the live 5m classifier's label at entry
    entry_slope               double precision,   -- stored 5m entry slope (completed-bar reference)
    entry_slope_direction     text,               -- up | down | flat (sign of entry_slope)
    realized_price_direction  text,               -- up | down | flat (sign of exit-entry, ground truth)
    direction_correct         boolean,            -- did price move the way the signal predicted
    entry_price               double precision,
    exit_price                double precision,
    realized_r                double precision,    -- net-of-cost R = net_pnl / (size_usd * init_stop_dist)
    net_pnl                   double precision,    -- realized pnl, already net of the real cost
    mfe_pct                   double precision,
    mae_pct                   double precision,
    hold_minutes              double precision,
    close_reason              text,
    created_at                timestamptz not null default now()
);
create index if not exists signal_outcomes_trade_idx on signal_outcomes (trade_id);
create index if not exists signal_outcomes_strategy_idx on signal_outcomes (strategy_id);

-- ROLLBACK (operator-run):
--   drop table if exists signal_outcomes;
