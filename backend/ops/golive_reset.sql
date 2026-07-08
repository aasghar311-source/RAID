-- ============================================================================
-- RAID GO-LIVE RESET  —  run in the Supabase SQL editor (service role)
-- ============================================================================
-- Purpose: fresh trading slate for the first honest measurement window.
-- PRESERVES the data pipeline + operator settings + measure-only calibration;
-- RESETS all trading state + learning + the old run's logs so the equity curve
-- restarts clean at config.STARTING_EQUITY ($4,000).
--
-- RUN ORDER (see GO_LIVE_RUNBOOK.md): this is STEP 3, AFTER crypto is disabled
-- (operator_controls.crypto_enabled=false) and AFTER the backup (STEP 2).
--
-- PRESERVED (NOT touched by this script):
--   ohlcv_5m               -- OHLCV capture / backtest + C10 data (operator: keep)
--   operator_controls      -- crypto flag + operator settings (the disable persists through reset)
--   pair_liquidity_metrics -- §2 tier calibration (measure-only; keep for tier-stability continuity)
--   market_state_log       -- spine calibration log (measure-only; keep for continuity)
--   (any *_backup_golive tables created in STEP 2)
--
-- RESET (truncated, identities restarted): trading state + learning + run logs.
-- CASCADE only reaches children of these trading tables; the preserved tables above
-- do not reference them, so they are safe.
-- ============================================================================

DO $$
DECLARE
  t text;
  reset_tables text[] := ARRAY[
    'trades',                 -- all paper trades (equity curve rebuilds from empty -> $4,000)
    'equity_snapshots',       -- equity curve  (empty => get_equity() re-seeds STARTING_EQUITY)
    'signals',                -- generated signals
    'pending_signals',        -- queued signals
    'signal_outcomes',        -- signal->outcome labels
    'cost_estimates',         -- per-trade cost estimates
    'position_quote_paths',   -- exit-replay quote paths
    'daily_stats',            -- per-day pnl / counters (kill-switch daily-loss reads this)
    'drawdown_state',         -- drawdown tier / peak tracking
    'sizing_state',           -- daily equity base (compounding) state
    'kill_switch',            -- empty => defaults to KILL_SWITCH_ACTIVE=False (not killed)
    'worker_leases',          -- single-worker lease (deploy re-acquires cleanly)
    'regime_log',             -- regime history (regenerates)
    'brain_decisions',        -- legacy brain decision log
    'predictions',            -- legacy prediction log
    'learning_adjustments',   -- learning state from the OLD negative-expectancy run (must reset)
    'goal_tracker',           -- goal tracking
    'latest_news'             -- news cache (regenerates)
  ];
BEGIN
  FOREACH t IN ARRAY reset_tables LOOP
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = 'public' AND table_name = t) THEN
      EXECUTE format('TRUNCATE TABLE public.%I RESTART IDENTITY CASCADE', t);
      RAISE NOTICE 'RESET  %', t;
    ELSE
      RAISE NOTICE 'skip (absent)  %', t;
    END IF;
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- Post-reset verification (should all be 0 rows for reset tables, >0 preserved)
-- ---------------------------------------------------------------------------
SELECT 'trades'                 AS tbl, count(*) FROM public.trades
UNION ALL SELECT 'equity_snapshots',       count(*) FROM public.equity_snapshots
UNION ALL SELECT 'kill_switch',            count(*) FROM public.kill_switch
UNION ALL SELECT 'worker_leases',          count(*) FROM public.worker_leases
UNION ALL SELECT 'ohlcv_5m (PRESERVED)',   count(*) FROM public.ohlcv_5m
UNION ALL SELECT 'operator_controls (PRESERVED)', count(*) FROM public.operator_controls
ORDER BY tbl;

-- Confirm crypto is still DISABLED going into the deploy (set in STEP 1; preserved here).
SELECT crypto_enabled, stocks_enabled, kalshi_enabled, options_enabled
FROM public.operator_controls;
