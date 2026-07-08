-- Migration 010 — pair_liquidity_metrics (Appendix-C §2 metrics layer, C.6 — SHADOW measure-only)
-- =================================================================================================
-- Persists the 15 §2 volume/liquidity/cost metrics per pair per cycle, computed COMPLETED-candle +
-- USD-quote (raid.core.liquidity.compute_pair_liquidity). MEASURE-ONLY — feeds NO decision yet; the
-- C.7 tier classifier reads these and the C.8 gate enforces later. Also records volume_ratio_forming
-- (the pre-B.4 forming-bar ratio A.2 gates on today) so the completed-vs-forming shift is measurable.
-- Unavailable metrics are NULL (fail-closed). NEW TABLE (not an ALTER). Additive, idempotent,
-- operator-run. Written by db.persist_pair_liquidity from raid.runner.

create table if not exists pair_liquidity_metrics (
    id                     bigserial primary key,
    cycle_ts               timestamptz,
    symbol                 text,
    -- VOLUME (7) — USD-quote, completed-candle
    dollar_vol_24h         double precision,   -- 24h USD volume (ticker)
    dollar_vol_30d_median  double precision,   -- NULL: no 30d daily history fetched (UNAVAILABLE)
    dollar_vol_5m_median   double precision,   -- median completed-5m-bar USD volume
    latest_5m_vol_usd      double precision,   -- latest COMPLETED 5m bar USD volume
    volume_ratio           double precision,   -- latest completed 5m vol / trailing-20 avg
    zero_volume_rate       double precision,   -- fraction of recent completed bars with 0 volume
    low_volume_rate        double precision,   -- fraction of recent bars with own ratio < 0.35
    -- LIQUIDITY (5)
    spread_pct             double precision,   -- (ask-bid)/mid, real book
    depth_10bps_usd        double precision,   -- USD depth within +/-10bps of mid (top-3 walls)
    depth_25bps_usd        double precision,   -- USD depth within +/-25bps of mid (top-3 walls)
    slippage_p50           double precision,   -- VWAP slippage to fill the p50 reference order
    slippage_p90           double precision,   -- VWAP slippage to fill the p90 reference order
    -- COST (3)
    dynamic_cost_pct       double precision,   -- real-spread round-trip cost (buffer excluded)
    target_cost_multiple   double precision,   -- 1-ATR move / cost
    net_rr                 double precision,   -- reference 2R ATR-stop setup, net of cost
    -- B.4 completed-vs-forming instrumentation
    volume_ratio_forming   double precision,   -- forming-bar ratio A.2 gates on TODAY (pre-B.4)
    completed_bars         integer,
    created_at             timestamptz not null default now()
);
create index if not exists pair_liquidity_metrics_ts_idx on pair_liquidity_metrics (cycle_ts);
create index if not exists pair_liquidity_metrics_sym_idx on pair_liquidity_metrics (symbol);

-- ROLLBACK (operator-run):
--   drop table if exists pair_liquidity_metrics;
