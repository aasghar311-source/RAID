-- ============================================================================
-- RAID Omega — Migration 001: immutable legacy trades archive (Phase 1, §19)
-- ============================================================================
-- PURPOSE
--   Preserve the entire pre-rebuild `trades` ledger in an IMMUTABLE, era-tagged
--   archive so new clean scorecards (Phase 2+) can EXCLUDE legacy data while the
--   historical record stays intact forever. Original IDs, timestamps, and all
--   fields are preserved. Historical PnL is NEVER rewritten.
--
-- SAFETY
--   * Additive-only. Does NOT modify or delete any row in `trades` or the existing
--     `trades_archive` table. Creates one new table + two read-only views.
--   * Idempotent. Safe to re-run: the table is created only if absent, and rows
--     are inserted only if their id is not already archived.
--   * Anon key CANNOT run DDL via PostgREST — run this in the Supabase SQL editor
--     (service role) once, manually, after reviewing.
--
-- CONTEXT
--   `trades`          — live ledger, ~441 rows, a MIX of every strategy config
--                       iterated this session (pre_omega era). Becomes frozen
--                       legacy once the rebuilt engine writes to new Phase-2 tables.
--   `trades_archive`  — an EARLIER manual archive (~542 rows) from prior resets.
--                       Left untouched; this migration captures the CURRENT trades
--                       ledger, which that earlier archive does not contain.
-- ============================================================================

-- 1. Immutable archive table: same shape as `trades`, plus provenance columns.
--    LIKE copies columns + types + NOT NULL, but NOT identity/defaults, so the
--    original `id` values are inserted verbatim (no identity conflict).
create table if not exists legacy_trades_archive (
    like trades
);

alter table legacy_trades_archive
    add column if not exists era         text        default 'pre_omega_rebuild',
    add column if not exists archived_at  timestamptz default now();

-- 2. Idempotent snapshot copy: only rows not already archived (match on id).
--    Positional insert: legacy_trades_archive columns == trades columns, then
--    era, then archived_at — so SELECT t.*, <era>, now() lines up exactly.
insert into legacy_trades_archive
select t.*, 'pre_omega_rebuild'::text, now()
from trades t
where not exists (
    select 1 from legacy_trades_archive a where a.id = t.id
);

-- 3. Read-only separation views for scorecards.
--    Legacy = anything captured in this archive. Production (clean) = trades in the
--    live table that are NOT in the archive, i.e. created AFTER the rebuild snapshot.
--    Production is empty today and fills only as the rebuilt engine trades.
create or replace view v_legacy_trades as
    select * from legacy_trades_archive;

create or replace view v_production_trades as
    select t.* from trades t
    where not exists (
        select 1 from legacy_trades_archive a where a.id = t.id
    );

-- 4. VERIFICATION (run after; expect archived == live trades count, production == 0).
--    select
--        (select count(*) from trades)                  as live_trades,
--        (select count(*) from legacy_trades_archive)   as archived,
--        (select count(*) from v_production_trades)     as clean_production,
--        (select count(*) from trades_archive)          as prior_manual_archive;

-- ============================================================================
-- ROLLBACK (fully reversible — removes only what this migration created):
--   drop view if exists v_production_trades;
--   drop view if exists v_legacy_trades;
--   drop table if exists legacy_trades_archive;
-- ============================================================================
--
-- IMMUTABILITY CONVENTION: never UPDATE or DELETE rows in legacy_trades_archive.
-- The rebuilt engine writes new trades to the Phase-2 strategy tables, never here.
-- ============================================================================
