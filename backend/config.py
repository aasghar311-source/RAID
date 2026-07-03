"""RAID configuration — every tunable lives here. Runtime values come from operator_controls.
These are fallback defaults only — the dashboard can override them without redeployment."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load the repo-root .env by EXPLICIT path. config.py lives in backend/, so the
# project root is one level up. Using an explicit path (instead of load_dotenv's
# caller-relative find_dotenv search) prevents a stray backend/.env from
# shadowing the real root .env. On Railway no .env exists (it is gitignored), so
# this no-ops and the dashboard-injected environment variables are used instead.
_ROOT_ENV = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ROOT_ENV)

# --- Identity -------------------------------------------------------------
BOT_NAME = "RAID"

# --- Capital & goal -------------------------------------------------------
STARTING_EQUITY        = 4000.0
FLOOR_TARGET           = 155_000.0
SUPERSONIC_TARGET      = 1_000_000.0

# --- Mode -----------------------------------------------------------------
# PAPER MODE IS PERMANENT for the Omega rebuild. There is NO date-based auto-flip.
# Live activation must be an explicit, operator-approved, gated code change — never
# a passive time trigger. Do not re-add LIVE_DATE / BOT_LIVE_DATE.
PAPER_MODE             = True

# --- Engine cutover -------------------------------------------------------
# The deterministic raid/ engine (typed candidates, 10 strategies, risk manager)
# replaces the legacy LLM brain path. Set False for an IMMEDIATE emergency rollback
# to the old brain — both paths remain in the codebase until Phase-7 deep cleanup.
USE_NEW_ENGINE         = True   # False = revert to legacy brain path
WORKER_ID              = os.getenv("RAILWAY_REPLICA_ID") or os.getenv("HOSTNAME") or "worker-1"

# --- Brain / cycle --------------------------------------------------------
# 5-minute strategy cycle — deterministic engine, zero API cost, so faster cycles = more
# opportunities. NOTE: operator_controls.brain_cycle_minutes OVERRIDES this at runtime;
# the worker syncs the DB to this value at startup (see worker.main).
BRAIN_CYCLE_MINUTES    = 5
MAX_OPEN_TRADES        = 60
MAX_ENTRIES_PER_CYCLE  = 30
CLAUDE_DAILY_BUDGET_USD = 7.0
CLAUDE_MODEL           = "claude-haiku-4-5-20251001"

# --- Kelly / sizing -------------------------------------------------------
KELLY_FRACTION_DEFAULT        = 0.40
TARGET_VOLATILITY             = 0.15        # vol scalar denominator
MIN_TRADE_SIZE_PCT            = 0.025       # 2.5% equity floor (Path B)
MAX_TRADE_SIZE_PCT            = 0.05        # 5% equity BASE (margin) per trade (~$200 now)
MAX_TRADE_SIZE_PCT_BEHIND     = 0.05        # 5% equity cap (~$200 now, auto-grows w/ equity)

# --- Leverage (3x aggressive sizing with drawdown-based de-risking) --------
# Leverage scales the position NOTIONAL, not the risk/base calc: $200 base x 3x = $600
# notional using $200 margin. The 95% deployment cap counts MARGIN (not notional), so the
# max concurrent-position count is unchanged (~19) but each position is 3x the exposure.
LEVERAGE_MULTIPLIER = 3       # 3x leverage on all positions
MAX_LEVERAGE        = 5       # absolute ceiling (never exceeded)
# Drawdown (from peak equity) reduces leverage, then pauses, then hard-stops:
LEVERAGE_DERISKING = {
    0.06: 2,     # 6% drawdown  -> 2x
    0.10: 1,     # 10% drawdown -> 1x (no leverage)
    0.15: 0,     # 15% drawdown -> pause all entries
    0.20: -1,    # 20% drawdown -> hard shutdown (kill switch)
}
HIGH_CONVICTION_THRESHOLD     = 0.72        # prob floor for size boost when BEHIND
CRITICAL_CONVICTION_THRESHOLD = 0.78        # prob floor for size boost when CRITICAL

# --- Correlated pair groups (apply -50% size if 3+ open in same group) ---
CORRELATED_PAIRS = [
    ["SOLUSD", "ETHUSD", "BTCUSD", "XRPUSD"],
    ["XLMUSD", "XMRUSD", "XDGUSD"],
]

# --- Operator timezone ----------------------------------------------------
OPERATOR_TZ = "America/Chicago"

# --- Risk limits ----------------------------------------------------------
DAILY_LOSS_LIMIT_PCT          = 0.10
CONSECUTIVE_LOSS_PAUSE        = 3
CONSECUTIVE_LOSS_PAUSE_MINUTES = 60
KALSHI_MAX_OPEN               = 4

# --- Markets (Phase 1 — crypto only; later phases flip via operator_controls) --
CRYPTO_ENABLED     = True
KALSHI_ENABLED     = False
STOCKS_ENABLED     = False
OPTIONS_ENABLED    = False
COMMODITIES_ENABLED = False

PENDING_SIGNALS_ENABLED = False
PENDING_SIGNAL_EXPIRY_MIN = 5   # unused by the deterministic engine; aligned to the 5-min cycle for the dashboard

# --- Scan / exit cadence --------------------------------------------------
LOOP_SLEEP_SECONDS    = 1     # exit monitor always runs at 1-second resolution
CONSECUTIVE_LOSS_LOOKBACK = 50
# Post-close per-symbol cooldown: at 5-min cycles, block re-entering a symbol for this many
# minutes after a trade on it closes (prevents churning the same stale setup). 15m = 3 cycles.
SYMBOL_COOLDOWN_MINUTES = 15

# --- Macro event handling -------------------------------------------------
MACRO_PAUSE_MINUTES_BEFORE = 30
MACRO_RESUME_MINUTES_AFTER = 15

# --- SL/TP (executor uses these for adverse-move overrides) ---------------
STOP_LOSS_PCT      = 0.01     # 1.0% fixed SL — backtester Config I
TAKE_PROFIT_PCT    = 0.04
MAX_SL_DISTANCE_PCT = 0.01    # 1.0% fixed SL — backtester Config I (floor==ceiling, no band)
MAX_TP_DISTANCE_PCT = 0.025   # 2.5% max TP distance — was avg 4.62%, 0/314 hit
KALSHI_SL_PCT      = 0.50
KALSHI_TP_PRICE    = 0.95
TRAIL_TRIGGER_PCT  = 0.015   # 1.5% — late trail, insurance only. TP at 2.5% is primary exit
TRAIL_STEP_PCT     = 0.005   # 0.5% — room for normal crypto pullbacks (was 0.3%, too tight)
ADVERSE_MOVE_PCT   = 0.04   # override fires only on violent moves (2x stop), not at stop level
AI_OVERRIDE_EXIT_ENABLED = True   # set False to fully disable discretionary exits
MAX_HOLD_HOURS = 3           # final forced close — tightened from 6h (0/20 MAT exits won)
# Matured exit (MAT) system — time-based checkpoints
MAT_CHECKPOINT_HOURS = 2     # first checkpoint: close if nicely profitable (was 4h)
MAT_PROFIT_PCT = 0.005       # 0.5% profit threshold at checkpoint — lower bar for faster exit
MAT_BREAKEVEN_PCT = 0.0025   # 0.25% covers round-trip fees — close as breakeven
MAX_HOLD_EXIT_ENABLED = True # set False to disable stale-trade exits
# No-progress exit: cut stalled trades BEFORE the 3h MAT death. 124-trade review: 28
# trades sat ~3h doing nothing then MAT-closed at -$2.04 avg; a trade not at least +0.3%
# by 90 min almost never recovers. Cutting at 90 min (~-$1.00 avg) saves ~$1/dead trade.
# Uses CURRENT gain vs entry (not peak) — see raid.execution.time_stops.no_progress_exit_due.
NO_PROGRESS_EXIT_ENABLED  = True    # enabled 2026-07-03 per 124-trade review
NO_PROGRESS_CHECK_MINUTES = 90      # check at 1.5h
NO_PROGRESS_MIN_GAIN_PCT  = 0.003   # 0.3% current gain required to stay open past the check

# --- EOD close (Phase 2 stocks/options) -----------------------------------
EOD_CLOSE_HOUR = 16
EOD_CLOSE_TZ   = "America/New_York"

# --- Claude cost constants ------------------------------------------------
CLAUDE_INPUT_COST_PER_TOKEN  = 0.000001   # $1.00 / 1M (claude-haiku-4-5)
CLAUDE_OUTPUT_COST_PER_TOKEN = 0.000005   # $5.00 / 1M (claude-haiku-4-5)

# --- Alert threshold (budget guard for Resend alert) ----------------------
CLAUDE_BUDGET_ALERT_AT = 6.0   # alert when spend exceeds $6 of $7

# --- Scanner tuning -------------------------------------------------------
KRAKEN_OHLC_INTERVAL  = 5        # minutes per candle
KRAKEN_MAX_PAIRS      = 0        # Disabled — volatile pairs only
PRIORITY_PAIRS = [
    # Tier 1 — high volatile + liquid (>5% range, >$1M vol)
    "RAVEUSD", "SYNUSD", "SLXUSD", "GWEIUSD", "AAVEUSD",
    "ZECUSD", "AVAXUSD", "NEARUSD", "SOLUSD",
    # Tier 2 — volatile + tradeable (>5% range)
    "FARTCOINUSD", "WIFUSD", "SPXUSD", "PENDLEUSD", "AEROUSD",
    "TIAUSD", "JUPUSD", "INJUSD", "FILUSD", "ENAUSD",
    # Tier 3 — moderate volatile (watchlist)
    "APTUSD", "SUIUSD", "RENDERUSD", "HYPEUSD", "ONDOUSD",
]
OHLCV_CANDLES         = 300      # candles per pair fetched
KRAKEN_QUOTES         = ("ZUSD", "USD")
MIN_24H_USD_VOLUME    = 1_000_000
KRAKEN_TICKER_CHUNK   = 200
KALSHI_CLOSE_WITHIN_HOURS = 24
NEWS_ENABLED          = False   # CryptoCompare disabled (rate-limited); deterministic engine uses no news
NEWS_LOOKBACK_HOURS   = 2
NEWS_TOP_N            = 3
HTTP_TIMEOUT          = 20.0
BULLISH_WORDS = (
    "surge", "rally", "breakout", "bullish", "buy", "up", "gain", "rise", "positive",
)
BEARISH_WORDS = (
    "crash", "drop", "bearish", "sell", "down", "loss", "fall", "decline", "negative", "fear",
)

# --- Technical indicator parameters (kept for signals.py math functions) --
RSI_PERIOD       = 14
RSI_OVERSOLD     = 30
RSI_OVERBOUGHT   = 70
EMA_FAST         = 20
EMA_MID          = 50
EMA_SLOW         = 200
MACD_FAST        = 12
MACD_SLOW        = 26
MACD_SIGNAL      = 9
VOLUME_CONFIRM_MULT = 1.5

# --- Learning (kept for worker backward compat; superseded by sizing_state) --
LEARNING_ENABLED       = False    # turned off — brain v2 uses Kelly/sizing_state
LEARNING_INTERVAL_DAYS = 7        # only LEARNING_* constant still read (worker cadence check)

# --- Ops ------------------------------------------------------------------
HEALTH_CHECK_PORT = 8080

# --- Legacy constants (kept so executor.py / gate.py import without error) --
BASE_TRADE_SIZE    = 100.0
RISK_REWARD_RATIO  = 2.0
MIN_CONFIDENCE     = 0.65
MIN_FACTOR_COUNT   = 4      # Minimum YES factors to trade (data: 4+=breakeven, 5+=profitable)
MAX_EQUITY_DEPLOYED_PCT = 0.95   # Path B: never deploy >95% of equity in open positions
EXCLUDED_SYMBOLS = ["GBPUSD", "EURUSD", "PAXGUSD", "XAUTUSD", "XAUUSD", "GBP", "EUR"]  # non-crypto, exclude from universe
CLAUDE_GRAY_ZONE_MIN    = 0.70
CLAUDE_GRAY_ZONE_MAX    = 0.80
CLAUDE_SKIP_THRESHOLD   = 0.85
CLAUDE_BUDGET_DAILY     = CLAUDE_DAILY_BUDGET_USD   # alias
BUDGET_TECH_THRESHOLD   = 75.0
CLAUDE_MAX_TOKENS       = 100
KILL_SWITCH_ACTIVE      = False
MAX_ENTRIES_PER_CYCLE   = 30
KALSHI_YES_LOW  = 0.30
KALSHI_YES_HIGH = 0.70
KALSHI_SKIP_LOW  = 0.35
KALSHI_SKIP_HIGH = 0.65
KALSHI_BASE_CONF = 0.75
KALSHI_VOLUME_BOOST_THRESHOLD = 10000
KALSHI_VOLUME_BOOST   = 0.05
KALSHI_TIME_URGENCY_HOURS = 2
KALSHI_TIME_BOOST     = 0.05
CONFIDENCE_CAP        = 0.95
NEWS_BOOST_ALIGNED    = 0.05
NEWS_PENALTY_OPPOSED  = 0.10
NEWS_BLOCK_FLOOR      = 0.60
CONF_MULT = {0.70: 1.0, 0.80: 1.2, 0.90: 1.5, 1.00: 2.0}
EQUITY_TIER_MULT = [(5000, 1.0), (20000, 1.5), (50000, 2.0), (float("inf"), 3.0)]
CRYPTO_SCAN_INTERVAL  = BRAIN_CYCLE_MINUTES * 60
KALSHI_SCAN_INTERVAL  = BRAIN_CYCLE_MINUTES * 60

# --- API keys (from .env, never hardcoded) --------------------------------
KRAKEN_API_KEY    = os.getenv("KRAKEN_API_KEY")
KRAKEN_API_SECRET = os.getenv("KRAKEN_API_SECRET")
KALSHI_API_KEY    = os.getenv("KALSHI_API_KEY")
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
NEWS_API_KEY      = os.getenv("NEWS_API_KEY")
SUPABASE_URL      = os.getenv("SUPABASE_URL")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY")
RESEND_API_KEY    = os.getenv("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")
OPERATOR_EMAIL    = os.getenv("OPERATOR_EMAIL", "aasghar311@gmail.com")

_REQUIRED_KEYS = (
    "KRAKEN_API_KEY",
    "KRAKEN_API_SECRET",
    "ANTHROPIC_API_KEY",
    "NEWS_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_KEY",
)


def validate_config():
    """Raise ValueError naming the first required key that is missing or empty."""
    for key in _REQUIRED_KEYS:
        if not globals().get(key):
            raise ValueError(f"Missing required config key: {key}")
    return True
