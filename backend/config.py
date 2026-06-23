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
PAPER_MODE             = True
LIVE_DATE              = "2026-07-20"
BOT_LIVE_DATE          = "2026-07-20"      # backward compat alias

# --- Brain / cycle --------------------------------------------------------
BRAIN_CYCLE_MINUTES    = 30
MAX_OPEN_TRADES        = 25
MAX_ENTRIES_PER_CYCLE  = 5
CLAUDE_DAILY_BUDGET_USD = 7.0
CLAUDE_MODEL           = "claude-sonnet-4-6"

# --- Kelly / sizing -------------------------------------------------------
KELLY_FRACTION_DEFAULT        = 0.25
TARGET_VOLATILITY             = 0.15        # vol scalar denominator
MIN_TRADE_SIZE_PCT            = 0.005       # 0.5% equity floor
MAX_TRADE_SIZE_PCT            = 0.05        # 5% equity cap (normal)
MAX_TRADE_SIZE_PCT_BEHIND     = 0.07        # 7% equity cap (BEHIND or CRITICAL)
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

# --- Scan / exit cadence --------------------------------------------------
LOOP_SLEEP_SECONDS    = 1     # exit monitor always runs at 1-second resolution
CONSECUTIVE_LOSS_LOOKBACK = 50

# --- Macro event handling -------------------------------------------------
MACRO_PAUSE_MINUTES_BEFORE = 30
MACRO_RESUME_MINUTES_AFTER = 15

# --- SL/TP (executor uses these for adverse-move overrides) ---------------
STOP_LOSS_PCT      = 0.02
TAKE_PROFIT_PCT    = 0.04
KALSHI_SL_PCT      = 0.50
KALSHI_TP_PRICE    = 0.95
TRAIL_TRIGGER_PCT  = 0.01
TRAIL_STEP_PCT     = 0.005
ADVERSE_MOVE_PCT   = 0.04   # override fires only on violent moves (2x stop), not at stop level
AI_OVERRIDE_EXIT_ENABLED = True   # set False to fully disable discretionary exits
MAX_HOLD_HOURS = 8           # close trades older than this if not already SL/TP'd (frees stuck slots)
MAX_HOLD_EXIT_ENABLED = True # set False to disable stale-trade exits

# --- EOD close (Phase 2 stocks/options) -----------------------------------
EOD_CLOSE_HOUR = 16
EOD_CLOSE_TZ   = "America/New_York"

# --- Claude cost constants ------------------------------------------------
CLAUDE_INPUT_COST_PER_TOKEN  = 0.000003   # $3.00 / 1M (claude-sonnet-4-6)
CLAUDE_OUTPUT_COST_PER_TOKEN = 0.000015   # $15.00 / 1M

# --- Alert threshold (budget guard for Resend alert) ----------------------
CLAUDE_BUDGET_ALERT_AT = 6.0   # alert when spend exceeds $6 of $7

# --- Scanner tuning -------------------------------------------------------
KRAKEN_OHLC_INTERVAL  = 5        # minutes per candle
KRAKEN_MAX_PAIRS      = 25       # top N by volume
OHLCV_CANDLES         = 300      # candles per pair fetched
KRAKEN_QUOTES         = ("ZUSD", "USD")
MIN_24H_USD_VOLUME    = 1_000_000
KRAKEN_TICKER_CHUNK   = 200
KALSHI_CLOSE_WITHIN_HOURS = 24
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
LEARNING_INTERVAL_DAYS = 7
LEARNING_MIN_SAMPLE    = 50
LEARNING_LOW_WIN_RATE  = 0.40
LEARNING_HIGH_WIN_RATE = 0.65
LEARNING_WEIGHT_DOWN   = 0.8
LEARNING_WEIGHT_UP     = 1.2

# --- Ops ------------------------------------------------------------------
HEALTH_CHECK_PORT = 8080

# --- Legacy constants (kept so executor.py / gate.py import without error) --
BASE_TRADE_SIZE    = 100.0
RISK_REWARD_RATIO  = 2.0
MIN_CONFIDENCE     = 0.50
CLAUDE_GRAY_ZONE_MIN    = 0.70
CLAUDE_GRAY_ZONE_MAX    = 0.80
CLAUDE_SKIP_THRESHOLD   = 0.85
CLAUDE_BUDGET_DAILY     = CLAUDE_DAILY_BUDGET_USD   # alias
BUDGET_TECH_THRESHOLD   = 75.0
CLAUDE_MAX_TOKENS       = 100
KILL_SWITCH_ACTIVE      = False
MAX_ENTRIES_PER_CYCLE   = 5
PULLBACK_BAND_PCT       = 0.01
PULLBACK_LOOKBACK       = 12
PULLBACK_MIN_BOUNCE     = 0.005
RSI_LONG_BAND_LOW       = 45.0
RSI_LONG_BAND_HIGH      = 60.0
RSI_SHORT_BAND_LOW      = 40.0
RSI_SHORT_BAND_HIGH     = 55.0
TS_RSI_WEIGHT  = 25.0
TS_MACD_WEIGHT = 25.0
TS_EMA_STRONG  = 50.0
TS_EMA_MIXED   = 15.0
TS_VOLUME_WEIGHT = 10.0
TS_RSI_PENALTY = 15.0
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
