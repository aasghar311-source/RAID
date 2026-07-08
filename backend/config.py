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

# --- Hard paper-only safety flags (fail-closed) ---------------------------
# Explicit, fail-closed layer ON TOP of PAPER_MODE and the structural absence of any
# order path (verified: no AddOrder/place_order/create_order anywhere in the repo).
# Each flag defaults to its SAFE value and falls back to safe on any unset/unparseable
# env var. A live order is permitted ONLY if an operator deliberately flips ALL THREE
# (see live_orders_allowed() + executor._assert_live_orders_allowed). Adding these
# introduces NO live path — the boundary they create can only BLOCK, never enable.
def _fail_closed_bool(env_name: str, *, safe_default: bool) -> bool:
    """Parse a bool env var; return safe_default on unset/unparseable AND on any
    exception during parsing (a raising parser must never enable live — fail-closed)."""
    try:
        raw = os.getenv(env_name)
        if raw is None:
            return safe_default
        val = raw.strip().lower()
        if val in ("1", "true", "yes", "on"):
            return True
        if val in ("0", "false", "no", "off"):
            return False
        return safe_default  # unparseable -> safe
    except Exception:  # noqa: BLE001 — a raising parser fails closed
        return safe_default

PAPER_ONLY           = _fail_closed_bool("PAPER_ONLY", safe_default=True)
LIVE_TRADING_ENABLED = _fail_closed_bool("LIVE_TRADING_ENABLED", safe_default=False)
KRAKEN_LIVE_ENABLED  = _fail_closed_bool("KRAKEN_LIVE_ENABLED", safe_default=False)

def live_orders_allowed() -> bool:
    """True ONLY when an operator has deliberately disabled BOTH paper gates AND enabled
    BOTH live flags, verified by STRICT IDENTITY (`is True`/`is False`) so a truthy string
    ('true','1','yes') or truthy int can NEVER satisfy it, and PAPER_MODE=True alone forces
    a block. Any exception while evaluating the flags returns False (fail-closed)."""
    try:
        return (
            PAPER_MODE is False
            and PAPER_ONLY is False
            and LIVE_TRADING_ENABLED is True
            and KRAKEN_LIVE_ENABLED is True
        )
    except Exception:  # noqa: BLE001 — any evaluation error fails closed
        return False

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
# (Commit D) breadth: allow opening across many DIFFERENT symbols in one cycle (per-symbol
# dedupe keeps it to one candidate per symbol, so this ~= max distinct symbols opened/cycle).
# The real bound is the 95% MARGIN deployment cap, not this number.
MAX_ENTRIES_PER_CYCLE  = 40
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
# B1: persist the drawdown high-water mark (peak equity) + ladder/pause state in the DB
# (drawdown_state table, migration 005) so a worker restart/redeploy cannot clear a drawdown
# pause — the in-memory runner._peak_equity previously re-seeded to 0 on boot. True = load/write
# drawdown_state, falling back to in-memory if the table is absent or the db lacks the accessor
# (then behaviour is identical to the legacy in-memory-only path). False = legacy. Reversible.
PERSIST_DRAWDOWN_STATE = True
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
CONSECUTIVE_LOSS_PAUSE        = 3   # threshold for the consec-loss ALERT (not a pause anymore)
CONSECUTIVE_LOSS_PAUSE_MINUTES = 60
# (Commit E) The consecutive-loss AUTO-PAUSE was removed — the bot must not freeze on a normal
# loss streak. The only automated backstops now are the drawdown de-risk ladder (below) and the
# manual kill_switch. The consec-loss alert still fires at CONSECUTIVE_LOSS_PAUSE.
KALSHI_MAX_OPEN               = 4

# --- Concentration caps (open-time gate, NOT a sizing change) -------------
# Stop correlated same-symbol stacking (e.g. the SLXUSD C3-short 4-stack that multiplied one
# bad thesis into a ~-$20 loss cluster). Enforced in the runner OPEN path against a live count
# of currently-open positions. INITIAL values — calibrate after the 24h run.
MAX_OPEN_PER_SYMBOL_STRATEGY_DIRECTION = 2   # (Commit D) up to 2 per (symbol, strategy, direction)
MAX_OPEN_PER_SYMBOL_TOTAL              = 3   # (Commit D) up to 3 open on one symbol across strategies

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
# Fail-closed staleness guard: the exit monitor batches all crypto prices ONCE per tick
# then processes trades sequentially, so a trade late in a slow (starved) loop can act on
# a batch price that is already seconds old. If the price used for an exit decision is
# older than this, skip that trade's exit checks this tick and log — never trail or stop
# on stale data (Omega rule: fail closed). 30s >> the 1s loop, so this is a safety net,
# not a normal-path behavior change.
STALE_PRICE_SECONDS   = 30
# Exit decisions read the live QUOTE (bid for long exits, ask for short) instead of last-trade,
# which freezes for minutes between prints on illiquid pairs. Only trust the book when the
# spread is sane; a wider/crossed/one-sided book falls back to last-trade (fail closed).
MAX_EXIT_SPREAD_PCT   = 0.02   # 2% — above this the quote is untrusted; use last-trade instead
CONSECUTIVE_LOSS_LOOKBACK = 50
# B5: quote-path flight recorder — capture open-position quote evidence (bid/ask/mid/spread/
# effective exit/MFE/MAE/source/freshness) into position_quote_paths for exit replay. The 1s exit
# loop only APPENDS to an in-memory buffer; the batched DB write is fire-and-forget so the loop is
# never blocked. True = capture (self-disables if the table is absent). False = off. Reversible.
QUOTE_PATH_CAPTURE = True
# A.1 (ENFORCE): price entries at the REAL book spread, never the 0.0004 order-book fallback. When
# True, build_candidate computes net_rr from dynamic_round_trip_cost_pct(real_spread) (hard-floored
# at the 1.04% realized SSOT) and REJECTS any entry whose real spread is unknown/zero (fail-closed)
# or exceeds MAX_SPREAD_PCT_UNIVERSAL. False = legacy 0.0004 fallback + flat 1.04% cost. Reversible.
ENFORCE_REAL_SPREAD_DEPTH = True
MAX_SPREAD_PCT_UNIVERSAL  = 0.0025   # Appendix-C §3 universal hard maximum spread (0.25% of mid)
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
# ATR-scaled stop (replaces the flat clamp[1%,2%] on the ATR-based strategies). The stop must
# clear the pair's normal 1h candle noise, which the old ~1% stop did NOT on volatile pairs
# (measured: BONK 1% vs 2.4% ATR, PEPE 0.6% vs 2.3%). stop = 1.5x ATR%(14,1h), bounded so calm
# pairs aren't unrealistically tight and a spiking-ATR pair can't make an enormous stop.
ATR_STOP_MULT      = 1.5
ATR_STOP_MIN       = 0.006    # 0.6% floor
ATR_STOP_MAX       = 0.040    # 4.0% ceiling
# Entry data-quality gate (FAIL-CLOSED): reject any candidate whose latest 5m bar has NO traded
# volume — a zero-volume bar has no real market and any fill on it is fiction. Enforced once in
# helpers.build_candidate (the single candidate-construction chokepoint) via features.volume_ratio,
# which is None on missing/insufficient bars and 0.0 when the latest bar volume is 0. Default 0.0
# blocks ONLY zero/missing (a genuine small positive ratio passes); raise later to test a thin-
# volume threshold without another refactor. HARD-ZERO ONLY — not a thin filter.
MIN_VOLUME_RATIO   = 0.0
# C7 short sleeve (PAPER). Independent, runtime-checked flag: when True, C7 shorts the bottom-
# quintile laggard in a TREND_DOWN regime (mirror of C3's short path). OFF => C7 shorts stay
# shadow-only (no C7 shorts booked; C3 and C7-long unaffected). ON RECORD: enabling this REVERSES a
# deliberate risk decision to disable a sleeve with MEASURED NEGATIVE expectancy (the ~-$33
# C7-short-in-RANGE bleed). Operator-authorized to collect C7-short data on the fresh window;
# C7-short is measured independently via (strategy=RAID-C7, direction=short) and independently
# killable by flipping this to False. PAPER ONLY — no live orders, no leverage change.
# 2026-07-07 (Omega rebuild): RETURNED TO SHADOW. False => C7 bottom-quintile laggards are
# shadow-logged (_c7_shadow_shorts), never booked; C3 and C7-long are unaffected. Re-enable later
# through the promotion track once the market-state spine defines RISK_OFF. Reversible: flip to
# True. PAPER ONLY — no live orders, no leverage change.
C7_SHORT_ENABLED   = False
# TP scales off the per-pair stop to keep the entry gate HONEST after the real 1.04% round-trip:
# tp_dist = RR_TARGET_NET*(stop + cost) + cost -> net_rr == RR_TARGET_NET (>= every min_net_rr
# 1.20/1.25/1.30). RR is held at this honest target; the stop/TP DISTANCES vary per pair.
RR_TARGET_NET      = 1.35
# Graduated cost/R gate (Commit 2) — ATR-scaled-stop strategies ONLY (C1/C3/C5/C6/C7).
# For those, rr_honest_target_dist pins net_rr at 1.35 REGARDLESS of stop distance, so the
# net_rr gate is blind to the ABSOLUTE cost load. When 1R (the stop) is so tight the ~1.04%
# round-trip cost dominates it, the trade is structurally unwinnable. Gate on the realized
# stop distance (gross_risk = 1R), which equals cost/R's denominator exactly (timeframe-free):
#   cost/R = realized_round_trip_cost_pct / gross_risk
#   FATAL   cost/R >= 0.87  <=>  gross_risk <= 1.04%/0.87 = 1.20%  (reject; ~ATR_1h < 0.80%)
#   MARGINAL cost/R >= 0.69 <=>  gross_risk <= 1.04%/0.69 = 1.50%  (half size; ~ATR_1h < 1.00%)
# Structural-stop strategies (C2/C4/C10) are EXEMPT: their net_rr is not pinned, so their
# existing net_rr gate already prices cost in (e.g. C4: tight ~0.8% stop but ~5R reward).
COST_R_FATAL_RATIO       = 0.87   # cost >= this fraction of 1R -> reject
COST_R_MARGINAL_RATIO    = 0.69   # cost in [marginal, fatal) of 1R -> half size
COST_R_MARGINAL_SIZE_MULT = 0.5   # risk multiplier for the marginal band
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
# 2026-07-04 (Commit B, aggressive retune): universe = the top 40 most-VOLATILE margin-eligible
# USD pairs by ATR%(14) on 1h candles, filtered to >= $500K 24h USD volume. Selected from a
# public Kraken AssetPairs + Ticker + OHLC scan (132 margin-eligible USD pairs -> 45 above the
# volume floor -> top 40 by ATR%). Breadth is where the aggression comes from. Fail closed: a
# symbol absent from KRAKEN_MAX_LEVERAGE is NOT traded. Re-run the selection periodically.
PRIORITY_PAIRS = [
    "BONKUSD", "POPCATUSD", "VVVUSD", "PEPEUSD", "XPLUSD", "TONUSD", "SPXUSD", "FARTCOINUSD",
    "USELESSUSD", "WLDUSD", "ADAUSD", "AEROUSD", "TIAUSD", "DYDXUSD", "PENGUUSD", "XLMUSD",
    "PUMPUSD", "ZECUSD", "CRVUSD", "NEARUSD", "AAVEUSD", "INJUSD", "HYPEUSD", "SUIUSD",
    "HBARUSD", "XRPUSD", "FETUSD", "LTCUSD", "XMRUSD", "ONDOUSD", "XDGUSD", "SOLUSD",
    "DOTUSD", "AVAXUSD", "UNIUSD", "TAOUSD", "CCUSD", "BCHUSD", "LINKUSD", "ETHUSD",
]

# Per-pair Kraken max margin leverage (RAW, from public AssetPairs leverage arrays). RAID's
# effective leverage per trade = min(RAID target 3x after drawdown, this cap). A symbol NOT in
# this map is NOT margin-eligible -> not traded (fail closed). XLMUSD caps at 2x (still eligible).
KRAKEN_MAX_LEVERAGE = {
    "BONKUSD": 3, "POPCATUSD": 3, "VVVUSD": 3, "PEPEUSD": 5, "XPLUSD": 3, "TONUSD": 3,
    "SPXUSD": 3, "FARTCOINUSD": 5, "USELESSUSD": 3, "WLDUSD": 3, "ADAUSD": 10, "AEROUSD": 3,
    "TIAUSD": 3, "DYDXUSD": 3, "PENGUUSD": 3, "XLMUSD": 2, "PUMPUSD": 3, "ZECUSD": 5,
    "CRVUSD": 5, "NEARUSD": 3, "AAVEUSD": 5, "INJUSD": 3, "HYPEUSD": 5, "SUIUSD": 10,
    "HBARUSD": 5, "XRPUSD": 10, "FETUSD": 3, "LTCUSD": 10, "XMRUSD": 5, "ONDOUSD": 3,
    "XDGUSD": 10, "SOLUSD": 10, "DOTUSD": 5, "AVAXUSD": 10, "UNIUSD": 5, "TAOUSD": 5,
    "CCUSD": 3, "BCHUSD": 5, "LINKUSD": 10, "ETHUSD": 10,
}
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
MAX_ENTRIES_PER_CYCLE   = 40   # (Commit D) duplicate of the value above — kept in sync (40)
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
