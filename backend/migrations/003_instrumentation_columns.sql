-- ============================================================================
-- RAID Omega — Migration 003: per-trade instrumentation columns (Commit 1)
-- ============================================================================
-- Run manually in the Supabase SQL editor (service role). ADDITIVE + IDEMPOTENT +
-- fully ROLLBACKABLE. Every column is NEW and NULLABLE — no existing column is
-- dropped, renamed, or retyped, and no row is touched. Safe to run on the live DB.
--
-- APPLY THIS *BEFORE* deploying the Commit-1 backend code. The Commit-1 code writes
-- these columns on trade open/close; if the columns do not yet exist, PostgREST
-- rejects the insert/update and trade logging fails (fail-closed). Order:
--   1. Run this migration in Supabase.  2. Verify the columns exist (query at bottom).
--   3. THEN deploy the Commit-1 code.
--
-- REUSED (NOT re-added — already serve their purpose):
--   peak_pnl_pct  -> MFE magnitude   (updated in executor.monitor_positions)
--   market_regime -> regime_at_entry (written at open in runner)
--   close_reason  -> exit rung       (written at close in db.close_trade)
-- ============================================================================

-- Clean, immutable risk anchor (the trail mutates `sl`; these never change post-open) ----
alter table trades add column if not exists initial_stop_price        double precision;
alter table trades add column if not exists initial_stop_distance_pct  double precision;

-- ATR at entry (the stop basis). 1h ATR14 as a fraction of price; NULL if the 1h
-- feature was unavailable at entry. This is the measurement timeframe for cost/R.
alter table trades add column if not exists entry_atr_pct              double precision;

-- Excursions. Magnitude of the peak already lives in peak_pnl_pct; these add the
-- TIMING of the peak and the full adverse-excursion (MAE), tracked each mgmt cycle.
alter table trades add column if not exists mfe_minutes_from_entry     double precision;
alter table trades add column if not exists mae_pct                    double precision;
alter table trades add column if not exists mae_minutes_from_entry     double precision;

-- Close-time context.
alter table trades add column if not exists hold_minutes               double precision;
alter table trades add column if not exists regime_at_exit             text;

-- Entry conviction inputs (trivially available from the entry feature snapshot).
alter table trades add column if not exists ema20_dist_pct             double precision;
alter table trades add column if not exists ema50_dist_pct             double precision;
alter table trades add column if not exists entry_slope               double precision;
alter table trades add column if not exists volume_ratio               double precision;

-- Added for completeness; NOT yet populated (needs a rolling ATR history). Left NULL
-- until an ATR-percentile is computed — documented as a known gap, not fabricated.
alter table trades add column if not exists atr_percentile             double precision;

-- Column documentation (optional but keeps the schema self-describing).
comment on column trades.initial_stop_price       is 'Clean stop at OPEN (c.stop_price); immutable — the trail mutates sl, never this. Real R denominator.';
comment on column trades.initial_stop_distance_pct is '|entry-initial_stop_price|/entry at open. The clean 1R distance (fraction).';
comment on column trades.entry_atr_pct            is '1h ATR14 as fraction of price at entry (the atr_scaled_stop basis); NULL if 1h feature absent.';
comment on column trades.mfe_minutes_from_entry   is 'Minutes from open to the high-water peak (peak_pnl_pct holds the magnitude).';
comment on column trades.mae_pct                  is 'Max adverse excursion (most-negative unrealized %), tracked each management cycle.';
comment on column trades.mae_minutes_from_entry   is 'Minutes from open to the MAE.';
comment on column trades.hold_minutes             is 'close_time - open_time in minutes, written at close.';
comment on column trades.regime_at_exit           is 'Regime label at close; best-effort (executor exit path has no regime -> may be NULL).';
comment on column trades.ema20_dist_pct           is '(price-ema20)/ema20 at entry (5m feature).';
comment on column trades.ema50_dist_pct           is '(price-ema50)/ema50 at entry (5m feature).';
comment on column trades.entry_slope              is 'trend_slope (normalized LS slope) at entry (5m feature).';
comment on column trades.volume_ratio             is 'latest 5m bar volume / trailing 20-bar average at entry; NULL if insufficient bars.';
comment on column trades.atr_percentile           is 'RESERVED — rolling ATR percentile at entry; not yet computed (NULL).';

-- Verify (run after applying): expect 13 rows.
-- select column_name, data_type from information_schema.columns
--  where table_name = 'trades' and column_name in (
--    'initial_stop_price','initial_stop_distance_pct','entry_atr_pct',
--    'mfe_minutes_from_entry','mae_pct','mae_minutes_from_entry','hold_minutes',
--    'regime_at_exit','ema20_dist_pct','ema50_dist_pct','entry_slope','volume_ratio',
--    'atr_percentile')
--  order by column_name;

-- ============================================================================
-- ROLLBACK (additive-only, so a clean drop of exactly the new columns):
--   alter table trades
--     drop column if exists initial_stop_price,
--     drop column if exists initial_stop_distance_pct,
--     drop column if exists entry_atr_pct,
--     drop column if exists mfe_minutes_from_entry,
--     drop column if exists mae_pct,
--     drop column if exists mae_minutes_from_entry,
--     drop column if exists hold_minutes,
--     drop column if exists regime_at_exit,
--     drop column if exists ema20_dist_pct,
--     drop column if exists ema50_dist_pct,
--     drop column if exists entry_slope,
--     drop column if exists volume_ratio,
--     drop column if exists atr_percentile;
-- ============================================================================
