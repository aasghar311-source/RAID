-- Migration 011 — pair_liquidity_metrics.trailing20_vol_usd (Appendix-C §5-9 trailing-20 metric)
-- =================================================================================================
-- Adds the trailing-20-bar mean USD volume metric (raid.core.liquidity.trailing20_vol_usd) to the
-- C.6 metrics table so the C.7 tier classifier's §5-9 trailing20 minimum is persisted alongside the
-- other 15 metrics. Additive, idempotent (add column if not exists) on the session-new table 010.
-- MEASURE-ONLY. Written by db.persist_pair_liquidity from raid.runner._pair_liquidity_shadow.

alter table pair_liquidity_metrics add column if not exists trailing20_vol_usd double precision;

-- ROLLBACK (operator-run):
--   alter table pair_liquidity_metrics drop column if exists trailing20_vol_usd;
