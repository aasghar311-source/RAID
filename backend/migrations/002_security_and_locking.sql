-- ============================================================================
-- RAID Omega — Migration 002: distributed worker lease + staged RLS (Phase 9)
-- ============================================================================
-- Run manually in the Supabase SQL editor (service role). Idempotent + rollbackable.
-- Two independent parts: (A) the worker-lease table is safe to apply now; (B) the
-- RLS section is STAGED and intentionally left commented until the backend switches
-- to the service-role key (enabling RLS now would break the anon-key writes and
-- silently stop all trading — exactly the failure disable_rls.sql was written for).
-- ============================================================================

-- ---------------------------------------------------------------------------
-- PART A — Distributed worker lease (single-writer guarantee). SAFE TO RUN NOW.
-- ---------------------------------------------------------------------------
create table if not exists worker_leases (
    id          integer primary key,
    holder_id   text,
    expires_at  timestamptz,
    updated_at  timestamptz default now()
);

-- Seed the single lease row (id=1). No-op if it already exists.
insert into worker_leases (id, holder_id, expires_at)
values (1, null, now() - interval '1 minute')
on conflict (id) do nothing;

-- The backend acquires with an ATOMIC compare-and-set (row-level atomic), e.g.:
--   update worker_leases
--      set holder_id = :worker, expires_at = :new_expiry, updated_at = now()
--    where id = 1
--      and (holder_id is null or expires_at < now() or holder_id = :worker)
--   returning id;   -- returns a row only if THIS worker now holds the lease.
-- Two overlapping workers can never both get a returned row.

-- ---------------------------------------------------------------------------
-- PART B — Staged RLS plan. DO NOT UNCOMMENT until the backend uses the
-- SERVICE_ROLE key (service_role bypasses RLS). Sequence:
--   1. Add SUPABASE_SERVICE_KEY to Railway; point the backend Supabase client at it.
--   2. Verify backend writes still work (trades/signals persist).
--   3. Confirm the frontend uses only the ANON key (NEXT_PUBLIC_SUPABASE_ANON_KEY).
--   4. THEN run PART B to enable RLS: anon gets READ on display tables only, no writes.
-- ---------------------------------------------------------------------------
-- Display tables the dashboard reads (anon SELECT allowed):
-- do $$
-- declare t text;
-- begin
--   foreach t in array array[
--     'trades','trades_archive','pending_signals','equity_snapshots','daily_stats',
--     'regime_log','goal_tracker','sizing_state','predictions','operator_controls'
--   ] loop
--     execute format('alter table %I enable row level security;', t);
--     execute format('drop policy if exists anon_read on %I;', t);
--     execute format('create policy anon_read on %I for select to anon using (true);', t);
--   end loop;
-- end $$;
-- Note: NO insert/update/delete policy for anon on ANY table -> the frontend becomes
-- strictly read-only. The service-role backend bypasses RLS and keeps full write.
-- operator_controls is read-only to anon (no write policy) so the dashboard can never
-- flip the kill switch / mode from an exposed key.

-- ============================================================================
-- ROLLBACK:
--   Part A:  drop table if exists worker_leases;
--   Part B:  foreach display table: alter table <t> disable row level security;
--            drop policy if exists anon_read on <t>;
-- ============================================================================
