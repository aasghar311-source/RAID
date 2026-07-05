-- ============================================================================
-- RAID Omega — Migration 004: 5m OHLCV capture table (Option B, additive-only)
-- ============================================================================
-- Purpose: persist the raw 5m candles the bot ALREADY fetches each cycle so future
-- REGIME / SL-TP / EXIT-path changes can be BACKTESTED. Write-only observability — NO
-- trading code reads this table. Additive + idempotent + fully rollbackable: creates ONE
-- new table, touches NO existing table/column, and no existing row.
--
-- FAIL-CLOSED ORDERING (follow this order — do not reorder):
--   1. Run THIS migration in the Supabase SQL editor (service role).
--   2. Verify the table exists (verify query at the bottom).
--   3. Deploy the capture code. It stays INERT (writes nothing) until step 4.
--   4. Set env  OHLCV_CAPTURE_ENABLED=true  and restart the worker -> capture begins.
-- The capture writer ALSO self-disables (logs once, no-ops) if this table is absent, so a
-- premature deploy cannot crash a cycle — the PGRST205 is swallowed (fail-open on capture).
--
-- Row volume:  40 symbols x 288 cycles/day  ~= 11,520 unique 5m bars/day (~4.2M rows/yr).
-- Storage:     ~100-140 B/row incl. index -> ~1.2-1.6 MB/day -> ~450-580 MB/yr (estimate).
-- Retention:   NOT touched by the regime_log 48h trim (db.cleanup_regime_log targets
--              regime_log only). This table is meant to be RETAINED for backtesting; it
--              grows unbounded until an explicit retention policy is chosen (optional trim
--              at the bottom — operator-run, deliberately NOT wired into any cycle).
-- ============================================================================

create table if not exists ohlcv_5m (
    id           bigserial   primary key,
    symbol       text        not null,                    -- Kraken altname, e.g. 'ETHUSD'
    bar_ts       timestamptz not null,                    -- candle OPEN time (Kraken OHLC ts)
    open         double precision,
    high         double precision,
    low          double precision,
    close        double precision,
    volume       double precision,
    captured_at  timestamptz not null default now(),      -- when a cycle first recorded it
    constraint ohlcv_5m_symbol_bar_uniq unique (symbol, bar_ts)   -- idempotent upsert key
);

-- Backtest read pattern: one symbol over a time range.
create index if not exists ohlcv_5m_symbol_bar_idx on ohlcv_5m (symbol, bar_ts);

-- ---------------------------------------------------------------------------
-- VERIFY (run AFTER applying; expect the table + 9 columns):
--   select table_name from information_schema.tables
--    where table_schema = 'public' and table_name = 'ohlcv_5m';
--   select column_name, data_type from information_schema.columns
--    where table_name = 'ohlcv_5m' order by ordinal_position;
-- ---------------------------------------------------------------------------

-- ============================================================================
-- ROLLBACK (additive-only -> clean drop of exactly the new object):
--   drop table if exists ohlcv_5m;
-- ============================================================================

-- OPTIONAL retention (DO NOT run unless storage is a concern — this DELETES backtest
-- data). Operator-run only; NOT scheduled and NOT referenced by any cycle. Keep ~90 days
-- (~1.0-1.3M rows, ~110-145 MB):
--   delete from ohlcv_5m where bar_ts < now() - interval '90 days';
-- ============================================================================
