"""RAID configuration — every tunable the bot uses lives here, loaded from .env."""

import os

from dotenv import load_dotenv

load_dotenv()

# --- Identity -------------------------------------------------------------
BOT_NAME = "RAID"

# --- Capital & sizing -----------------------------------------------------
STARTING_EQUITY = 4000.0
BASE_TRADE_SIZE = 100.0
RISK_REWARD_RATIO = 2.0

# --- Risk limits ----------------------------------------------------------
DAILY_LOSS_LIMIT_PCT = 0.10
MAX_OPEN_TRADES = 20
KALSHI_MAX_OPEN = 4

# --- Confidence thresholds ------------------------------------------------
MIN_CONFIDENCE = 0.55
CLAUDE_GRAY_ZONE_MIN = 0.70
CLAUDE_GRAY_ZONE_MAX = 0.80
CLAUDE_SKIP_THRESHOLD = 0.85
CLAUDE_BUDGET_DAILY = 3.00
CLAUDE_MODEL = "claude-sonnet-4-6"

# --- Mode -----------------------------------------------------------------
PAPER_MODE = True
PAPER_MODE_UNTIL = "2026-07-20"
BOT_LIVE_DATE = "2026-07-20"

# --- Scan cadence (seconds) ----------------------------------------------
CRYPTO_SCAN_INTERVAL = 60
KALSHI_SCAN_INTERVAL = 60

# --- Circuit breakers -----------------------------------------------------
CONSECUTIVE_LOSS_PAUSE = 3
CONSECUTIVE_LOSS_PAUSE_MINUTES = 60
KILL_SWITCH_ACTIVE = False

# --- Macro event handling -------------------------------------------------
MACRO_PAUSE_MINUTES_BEFORE = 30
MACRO_RESUME_MINUTES_AFTER = 15

# --- Learning loop --------------------------------------------------------
LEARNING_ENABLED = True
LEARNING_INTERVAL_DAYS = 7

# --- Ops ------------------------------------------------------------------
HEALTH_CHECK_PORT = 8080

# --- Markets (Phase 1) ----------------------------------------------------
CRYPTO_ENABLED = True
KALSHI_ENABLED = True
STOCKS_ENABLED = False
OPTIONS_ENABLED = False
COMMODITIES_ENABLED = False

# --- Position sizing multipliers (used in executor) -----------------------
CONF_MULT = {0.70: 1.0, 0.80: 1.2, 0.90: 1.5, 1.00: 2.0}
EQUITY_TIER_MULT = [(5000, 1.0), (20000, 1.5), (50000, 2.0), (float("inf"), 3.0)]

# --- End-of-day close (stocks/options — Phase 2) --------------------------
EOD_CLOSE_HOUR = 16
EOD_CLOSE_TZ = "America/New_York"

# --- Claude / cost --------------------------------------------------------
CLAUDE_MAX_TOKENS = 100
CLAUDE_INPUT_COST_PER_TOKEN = 0.000003   # $3.00 / 1M tokens (claude-sonnet-4-6)
CLAUDE_OUTPUT_COST_PER_TOKEN = 0.000015  # $15.00 / 1M tokens (claude-sonnet-4-6)
BUDGET_TECH_THRESHOLD = 75.0             # technical-only ENTER bar when budget spent

# --- Scanner tuning -------------------------------------------------------
KRAKEN_OHLC_INTERVAL = 5      # minutes per candle
KRAKEN_MAX_PAIRS = 25         # cap pairs scanned per cycle
OHLCV_CANDLES = 300           # candles per pair (must exceed EMA_SLOW so EMA200 computes)
KRAKEN_QUOTES = ("ZUSD", "USD")
KALSHI_CLOSE_WITHIN_HOURS = 24
NEWS_LOOKBACK_HOURS = 2
NEWS_TOP_N = 3
HTTP_TIMEOUT = 20.0
BULLISH_WORDS = (
    "surge", "rally", "breakout", "bullish", "buy", "up", "gain", "rise", "positive",
)
BEARISH_WORDS = (
    "crash", "drop", "bearish", "sell", "down", "loss", "fall", "decline", "negative", "fear",
)

# --- Technical scoring (signals) ------------------------------------------
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_LONG_THRESHOLD = 35
RSI_SHORT_THRESHOLD = 65
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
VOLUME_CONFIRM_MULT = 1.5
TS_RSI_WEIGHT = 25.0
TS_MACD_WEIGHT = 25.0
TS_EMA_STRONG = 50.0
TS_EMA_MIXED = 15.0
TS_VOLUME_WEIGHT = 10.0

# --- Kalshi scoring (signals) ---------------------------------------------
KALSHI_YES_LOW = 0.30
KALSHI_YES_HIGH = 0.70
KALSHI_SKIP_LOW = 0.35
KALSHI_SKIP_HIGH = 0.65
KALSHI_BASE_CONF = 0.75
KALSHI_VOLUME_BOOST_THRESHOLD = 10000
KALSHI_VOLUME_BOOST = 0.05
KALSHI_TIME_URGENCY_HOURS = 2
KALSHI_TIME_BOOST = 0.05
CONFIDENCE_CAP = 0.95

# --- News adjustment (signals) --------------------------------------------
NEWS_BOOST_ALIGNED = 0.05
NEWS_PENALTY_OPPOSED = 0.10
NEWS_BLOCK_FLOOR = 0.60

# --- Risk geometry (executor) ---------------------------------------------
STOP_LOSS_PCT = 0.02
TAKE_PROFIT_PCT = 0.04
KALSHI_SL_PCT = 0.50          # 50% loss on Kalshi position value
KALSHI_TP_PRICE = 0.95        # near resolution
TRAIL_TRIGGER_PCT = 0.01      # move to breakeven once 1% in favor
TRAIL_STEP_PCT = 0.005        # trail by 0.5% per additional 0.5% move
ADVERSE_MOVE_PCT = 0.02       # >2% sudden adverse move triggers Claude override

# --- Learning -------------------------------------------------------------
LEARNING_MIN_SAMPLE = 50
LEARNING_LOW_WIN_RATE = 0.40
LEARNING_HIGH_WIN_RATE = 0.65
LEARNING_WEIGHT_DOWN = 0.8
LEARNING_WEIGHT_UP = 1.2

# --- Worker ---------------------------------------------------------------
LOOP_SLEEP_SECONDS = 1
CONSECUTIVE_LOSS_LOOKBACK = 50

# --- API keys (from .env, never hardcoded) --------------------------------
KRAKEN_API_KEY = os.getenv("KRAKEN_API_KEY")
KRAKEN_API_SECRET = os.getenv("KRAKEN_API_SECRET")
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY")
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

_REQUIRED_KEYS = (
    "KRAKEN_API_KEY",
    "KRAKEN_API_SECRET",
    "KALSHI_API_KEY",
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
