"""RAID brain v2 — Claude IS the trading brain.

Full decision cycle every 55 minutes:
  Step 1 — Goal pulse (trajectory math, log to goal_tracker)
  Step 2 — Market context (indicators compressed to <400 tokens/asset)
  Step 3 — Claude brain call (JSON response with trade decisions)
  Step 4 — Parse, gate, execute, log all new columns
  Step 5 — Sizing state update (Kelly / optimal-f)
"""

import ast
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

import alert_manager
import config
import gate
from signals import Signal, _ema_last, _macd, _rsi
from scanner import ScanResult

log = logging.getLogger("raid.brain")
CDT = ZoneInfo("America/Chicago")

_client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

_daily_spend = 0.0
_last_reset_date = datetime.now(timezone.utc).date().isoformat()
_last_brain_cycle_time: float = 0.0   # unix timestamp of last completed cycle
_consecutive_missed_cycles: int = 0
_last_trajectory_status = "ON_TRACK"  # surfaced to the worker health endpoint


def _extract_prob_from_reasoning(reasoning: str) -> float:
    """Recover probability from mandatory format 'Y:...=N P:...=M -> 0.XX' or '→ 0.XX'"""
    if not reasoning:
        return 0.0
    import re
    match = re.search(r'(?:→|->)\s*(0\.\d+)', reasoning)
    return float(match.group(1)) if match else 0.0


def _widen_tp(entry: float, sl: float, direction: str, target_rr: float = 1.5) -> float:
    """Calculate minimum TP to achieve target R:R ratio."""
    risk = abs(entry - sl)
    reward_needed = risk * target_rr
    if direction in ("long", "yes"):
        return entry + reward_needed
    else:
        return entry - reward_needed


# ── Backward-compat dataclass (used by executor._claude_override) ──────────

@dataclass
class BrainResult:
    decision: str
    confidence: float
    reasoning: str
    cost_usd: float
    skipped_budget: bool = False


# ── Spend tracking ────────────────────────────────────────────────────────

def reset_daily_spend():
    global _daily_spend, _last_reset_date
    _daily_spend = 0.0
    _last_reset_date = datetime.now(timezone.utc).date().isoformat()


def get_daily_spend():
    return _daily_spend


def set_daily_spend(value: float):
    """Seed in-memory spend from the persisted DB value on startup."""
    global _daily_spend, _last_reset_date
    _daily_spend = value
    _last_reset_date = datetime.now(timezone.utc).date().isoformat()


def get_trajectory_status():
    """Latest trajectory status from the most recent goal pulse (for health/UI)."""
    return _last_trajectory_status


def _check_reset():
    today = datetime.now(timezone.utc).date().isoformat()
    if today != _last_reset_date:
        reset_daily_spend()


# ── STEP 1: GOAL PULSE ────────────────────────────────────────────────────

async def _run_goal_pulse(db) -> dict:
    """Calculate trajectory math and log to goal_tracker. Returns trajectory dict."""
    global _last_trajectory_status
    try:
        # Strict read: None means the equity read FAILED (vs an empty table). Do
        # not size trades against a fabricated $4000 — abort entries this cycle.
        equity = await db.get_equity_strict()
        if equity is None:
            log.error("GOAL PULSE: equity read failed — entries aborted this cycle")
            return {
                "equity": config.STARTING_EQUITY,
                "equity_available": False,
                "trajectory_status": "ON_TRACK",
                "required_daily_return": 0.0,
                "current_daily_return": 0.0,
                "days_remaining": 0,
                "projected_hit_date": None,
            }
        now_cdt = datetime.now(CDT)
        eoy = datetime(2026, 12, 31, tzinfo=CDT)
        days_remaining = max(1, (eoy.date() - now_cdt.date()).days)

        required_daily_return = (
            (config.FLOOR_TARGET / equity) ** (1.0 / days_remaining) - 1
        ) if equity > 0 else 0.0

        # Compute current daily return from equity snapshots (prefer daily snapshots).
        current_daily_return = 0.0
        projected_hit_date = None
        try:
            history = await db.get_equity_history(days=30)
            if len(history) >= 2:
                # Geometric mean of daily returns from snapshot series.
                daily_returns = []
                for i in range(1, len(history)):
                    e_prev = float(history[i - 1]["equity"] or 0)
                    e_curr = float(history[i]["equity"] or 0)
                    if e_prev > 0:
                        daily_returns.append(e_curr / e_prev - 1)
                if daily_returns:
                    product = 1.0
                    for r in daily_returns:
                        product *= (1 + r)
                    current_daily_return = product ** (1.0 / len(daily_returns)) - 1
            else:
                # Fall back to last 30 closed trade returns.
                trades = await db.get_closed_trades_last_n(30)
                if trades:
                    trade_returns = []
                    for t in trades:
                        size = float(t.get("size_usd") or 0)
                        pnl = float(t.get("pnl") or 0)
                        if size > 0:
                            trade_returns.append(pnl / size)
                    if trade_returns:
                        product = 1.0
                        for r in trade_returns:
                            product *= max(0.0001, 1 + r)
                        current_daily_return = product ** (1.0 / len(trade_returns)) - 1
        except Exception as exc:  # noqa: BLE001
            log.error("goal_pulse return calc failed: %s", exc)

        # Trajectory classification.
        if required_daily_return > 0:
            ratio = current_daily_return / required_daily_return
        else:
            ratio = 1.0

        if ratio >= 1.2:
            trajectory_status = "AHEAD"
        elif ratio >= 0.9:
            trajectory_status = "ON_TRACK"
        elif ratio >= 0.5:
            trajectory_status = "BEHIND"
        else:
            trajectory_status = "CRITICAL"
        _last_trajectory_status = trajectory_status

        # Projected hit date at current trajectory.
        if current_daily_return > 0 and equity > 0:
            try:
                days_to_floor = math.log(config.FLOOR_TARGET / equity) / math.log(1 + current_daily_return)
                projected_hit_date = (now_cdt + timedelta(days=int(days_to_floor))).strftime("%Y-%m-%d")
            except Exception:  # noqa: BLE001
                projected_hit_date = None

        entry = {
            "current_equity": equity,
            "floor_target": config.FLOOR_TARGET,
            "supersonic_target": config.SUPERSONIC_TARGET,
            "days_remaining": days_remaining,
            "required_daily_return": required_daily_return,
            "current_daily_return": current_daily_return,
            "trajectory_status": trajectory_status,
            "projected_hit_date": projected_hit_date,
        }
        await db.log_goal_tracker(entry)

        log.info(
            "GOAL PULSE — equity=$%.2f days=%d req=%.2f%% curr=%.2f%% status=%s proj=%s",
            equity, days_remaining,
            required_daily_return * 100, current_daily_return * 100,
            trajectory_status, projected_hit_date,
        )

        if trajectory_status == "CRITICAL":
            await alert_manager.alert_critical_trajectory(
                equity, required_daily_return, current_daily_return, days_remaining
            )

        return {
            **entry,
            "equity": equity,
            "equity_available": True,
        }
    except Exception as exc:  # noqa: BLE001
        log.error("_run_goal_pulse failed: %s", exc)
        # On a goal-pulse crash, treat equity as unavailable so entries are skipped.
        return {
            "equity": config.STARTING_EQUITY,
            "equity_available": False,
            "trajectory_status": "ON_TRACK",
            "required_daily_return": 0.0,
            "current_daily_return": 0.0,
            "days_remaining": 192,
            "projected_hit_date": None,
        }


# ── STEP 2: MARKET CONTEXT ────────────────────────────────────────────────

def _macd_state(closes: list) -> str:
    """Return 'bullish', 'bearish', or 'neutral' from current MACD vs signal position."""
    ml, sl = _macd(closes)
    if not ml or not sl:
        return "neutral"
    diff = ml[-1] - sl[-1]
    if diff > 0:
        return "bullish"
    if diff < 0:
        return "bearish"
    return "neutral"


def _compute_realized_vol(ohlcv: list) -> float:
    """Annualized realized vol from 5-min candles (288 candles/day, 365 days/year)."""
    closes = [float(c[4]) for c in ohlcv if c[4] and float(c[4]) > 0]
    if len(closes) < 10:
        return 0.20
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if not log_returns:
        return 0.20
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / len(log_returns)
    return math.sqrt(variance) * math.sqrt(365 * 288)


def _compute_swing_levels(ohlcv: list, n: int = 3):
    """Return (swing_highs[-n:], swing_lows[-n:]) from local extrema."""
    if len(ohlcv) < 3:
        return [], []
    highs = [float(c[2]) for c in ohlcv]
    lows = [float(c[3]) for c in ohlcv]
    swing_highs, swing_lows = [], []
    for i in range(1, len(ohlcv) - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            swing_highs.append(round(highs[i], 6))
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            swing_lows.append(round(lows[i], 6))
    return swing_highs[-n:], swing_lows[-n:]


def _compute_24h_change_pct(ohlcv: list) -> float:
    closes = [float(c[4]) for c in ohlcv if c[4]]
    if not closes:
        return 0.0
    ref_idx = -min(289, len(closes))
    ref = closes[ref_idx]
    if ref <= 0:
        return 0.0
    return round((closes[-1] / ref - 1) * 100, 2)


def _htf_trend_label(ohlcv_1h: list) -> str:
    """Compress 1-hour candles to a single trend label: up / down / sideways.
    Based on price vs 1h EMA20/EMA50 — cheap, a few tokens for the prompt."""
    closes = [float(c[4]) for c in (ohlcv_1h or []) if c[4] and float(c[4]) > 0]
    if len(closes) < 20:
        return "unknown"
    ema20 = _ema_last(closes, 20)
    ema50 = _ema_last(closes, 50) if len(closes) >= 50 else ema20
    price = closes[-1]
    if price > ema20 and ema20 >= ema50:
        return "up"
    if price < ema20 and ema20 <= ema50:
        return "down"
    return "sideways"


def _build_asset_context(scan_result, news_info: dict) -> dict:
    """Build compressed per-asset context dict (<400 tokens) for Claude."""
    ohlcv = scan_result.ohlcv or []
    closes = [float(c[4]) for c in ohlcv if c[4]] if ohlcv else []
    price = float(scan_result.current_price or (closes[-1] if closes else 0))

    ema20 = _ema_last(closes, config.EMA_FAST) if closes else None
    ema50 = _ema_last(closes, config.EMA_MID) if closes else None
    ema200 = _ema_last(closes, config.EMA_SLOW) if closes else None
    rsi = _rsi(closes, config.RSI_PERIOD) if len(closes) > config.RSI_PERIOD else 50.0
    macd = _macd_state(closes) if closes else "neutral"
    swing_highs, swing_lows = _compute_swing_levels(ohlcv)
    vol = _compute_realized_vol(ohlcv)
    change_24h = _compute_24h_change_pct(ohlcv)

    now_cdt = datetime.now(CDT)

    # Smart rounding: 2dp for prices > $1, 6dp for sub-dollar coins.
    _rd = 2 if price > 1 else 6
    def _r(v):
        return round(v, _rd) if v is not None else None

    # Compress order book: [[price, usd_volume]] arrays, top 3 walls each side.
    ob_raw = getattr(scan_result, "order_book", {})
    ob = None
    if ob_raw:
        bw = ob_raw.get("bid_walls") or []
        aw = ob_raw.get("ask_walls") or []
        if bw or aw:
            ob = {
                "b": [[round(w["price"], _rd), int(w["usd"])] for w in bw[:3]],
                "a": [[round(w["price"], _rd), int(w["usd"])] for w in aw[:3]],
            }

    # Truncate headline to 80 chars.
    headline = (news_info or {}).get("headline")
    if headline and len(headline) > 80:
        headline = headline[:80]

    ctx = {
        "px": _r(price),
        "chg24": change_24h,
        "e20": _r(ema20),
        "e50": _r(ema50),
        "e200": _r(ema200),
        "rsi": round(rsi, 1),
        "macd": macd,
        "shi": swing_highs[:3],
        "slo": swing_lows[:3],
        "vol": round(vol, 4),
        "t1h": _htf_trend_label(getattr(scan_result, "ohlcv_1h", [])),
        "t30m": _htf_trend_label(getattr(scan_result, "ohlcv_30m", [])),
        "t15m": _htf_trend_label(getattr(scan_result, "ohlcv_15m", [])),
        "news": headline,
        "nsent": (news_info or {}).get("sentiment", "neutral"),
        "fr": round(getattr(scan_result, "funding_rate", 0.0), 6),
        "ob": ob,
        "oi": round(getattr(scan_result, "open_interest", 0.0), 2),
        "fg": getattr(scan_result, "fear_greed", 50),
        "hr": now_cdt.hour,
    }
    return ctx


def _build_market_context(scan_results: list, news_by_symbol: dict) -> dict:
    ctx = {}
    for sr in scan_results:
        try:
            news = news_by_symbol.get(sr.symbol, {})
            ctx[sr.symbol] = _build_asset_context(sr, news)
        except Exception as exc:  # noqa: BLE001
            log.error("build_asset_context failed for %s: %s", sr.symbol, exc)
    return ctx


def _build_open_positions_context(open_trades: list) -> list:
    return [
        {
            "symbol": t.get("symbol"),
            "direction": t.get("direction"),
            "entry_price": t.get("entry_price"),
            "size_usd": t.get("size_usd"),
            "market": t.get("market"),
        }
        for t in open_trades
    ]


def _build_recent_trades_context(recent_trades: list) -> list:
    return [
        {
            "symbol": t.get("symbol"),
            "direction": t.get("direction"),
            "pnl": round(float(t.get("pnl") or 0), 2),
            "close_reason": t.get("close_reason"),
            "market_regime": t.get("market_regime"),
            "claude_reasoning": t.get("claude_reasoning"),
        }
        for t in recent_trades[:5]
    ]


def _build_scorecard(trades: list) -> dict:
    """Compute the brain's OWN recent track record so it can self-correct.
    Breaks win rate down by direction, regime, and confidence bucket — lets
    the brain see e.g. 'my longs win 26%' or 'my 0.70 calls actually win 47%'
    and adjust THIS cycle accordingly. Reflection, not learning."""
    def _wr(rows):
        n = len(rows)
        if n == 0:
            return None
        w = sum(1 for r in rows if (r.get("pnl") or 0) > 0)
        return {"n": n, "win_pct": round(100.0 * w / n, 0),
                "avg_pnl": round(sum(float(r.get("pnl") or 0) for r in rows) / n, 2)}
    closed = [t for t in (trades or []) if t.get("pnl") is not None]
    by_dir, by_regime, by_conf = {}, {}, {}
    for t in closed:
        d = t.get("direction") or "?"
        by_dir.setdefault(d, []).append(t)
        rg = t.get("market_regime") or "?"
        by_regime.setdefault(rg, []).append(t)
        p = t.get("predicted_prob")
        if p is not None:
            b = str(round(float(p), 1))
            by_conf.setdefault(b, []).append(t)
    return {
        "overall": _wr(closed),
        "by_direction": {k: _wr(v) for k, v in by_dir.items()},
        "by_regime": {k: _wr(v) for k, v in by_regime.items()},
        "by_confidence": {k: _wr(v) for k, v in by_conf.items()},
    }


# ── STEP 3: CLAUDE BRAIN CALL ─────────────────────────────────────────────

_SYSTEM_PROMPT = """You are the trading brain for RAID.
Operator: Ali, Houston Texas (CDT timezone).
You have complete control. No filter overrides you.

DATA KEY LEGEND (market data uses compressed keys):
px=price, chg24=24h change%, e20/e50/e200=EMA periods, rsi=RSI14, macd=MACD state,
shi/slo=swing highs/lows (top 3), vol=30d volatility, t1h/t30m/t15m=timeframe trend,
news=headline (80ch), nsent=news sentiment, fr=funding rate, ob=order book
(b=bids a=asks as [[price,usd]]), oi=open interest, fg=fear&greed (0-100), hr=CDT hour.

MISSION
You are RAID's autonomous trading brain. Your job: run the checklist, score every asset honestly, and output ALL signals with probability >= 0.65.
Do NOT self-filter. Do NOT skip signals based on trajectory status. Do NOT raise your own quality bar above 0.65.
The system gates (R:R check, SL band, deployment cap, daily loss limit) handle risk management — not you.
More signals = more data = better system. Every valid signal matters.

SIZING RULES
- probability >= 0.80: size_pct = 5.0
- probability >= 0.75: size_pct = 4.5
- probability >= 0.70: size_pct = 4.0
- probability >= 0.65: size_pct = 3.0
Do NOT adjust sizing based on trajectory status. Do NOT reference kelly_fraction for sizing.
Correlation penalty is applied code-side.
Honesty rule: probability scoring must NEVER bend to justify a desired size. Score first, size follows.

FEES (non-negotiable — factor into every single trade):
Kraken taker fee: 0.16% per side = 0.32% round trip.
Fee cost = size_usd × 0.0032
Rules:
- Minimum TP distance from entry: 1.0% (covers fee + minimum profit)
- Never set TP below 0.8% from entry — fee drag makes it not worth it
- True net R:R = (TP_pct - 0.32%) / (SL_pct + 0.32%)
- Always include estimated fee cost in your sizing_note

ANALYSIS PROCESS:
1. Detect regime per asset:
   TRENDING_UP:   EMA aligned up, price above EMA20
   TRENDING_DOWN: EMA aligned down, price below EMA20
   SIDEWAYS:      Price within 1.5% of EMA20, low vol
   VOLATILE:      vol_30d > 2× typical (>0.40 for crypto)
   EVENT_DRIVEN:  Major macro event within 24 hours
   NO_EDGE:       Unclear — skip

2. Entry rules:
   TRENDING_UP:   long on pullback to EMA20 — never chase >3% from EMA20
   TRENDING_DOWN: SHORT is the primary direction — look for short entries
                  only take longs if RSI < 25 and bounce evidence is strong
   SIDEWAYS:      both directions valid — prob > 0.65
   VOLATILE:      prefer shorts (asymmetric downside) — prob > 0.72
   NO_EDGE:       always skip

3. Capital allocation by regime:
   TRENDING:     up to 60% equity, 3–6 positions
   SIDEWAYS:     up to 20% equity, 1–2 positions
   VOLATILE:     up to 30% equity, 2-4 positions, prob > 0.72
   EVENT_DRIVEN: up to 40% equity on the event

   DIRECTIONAL JUDGMENT (you decide — no forced trades):
   - Count open longs and open shorts before every entry
   - 60% same-direction cap: never let one direction exceed 60% of open book
     (this is a risk limit, NOT a mandate to take the opposite trade — if the
     only setups are on the capped side, SKIP rather than force a bad trade)
   - Direction must follow regime, not a quota:
       In a confirmed downtrend, longs are COUNTER-TREND bets — take them only
       with exceptional evidence (strong oversold reversal, clear support reclaim).
       Default to shorts or skip. Do not go long just to balance the book.
       In a confirmed uptrend, the inverse applies.
   - Always analyze BOTH long and short setups for every symbol, then pick the
     one the regime actually supports — or neither.
   - Diversify across tiers: large cap (BTC/ETH), mid cap (SOL/ADA/XRP),
     small cap (SYN/XMR/LINK) — never more than 3 from same tier

4. Market session (CDT):
   PAPER MODE: trade 24/7 — accumulate HONEST data, no session restrictions
   Best live hours: 8am–12pm, 8pm–12am (enforced after July 20 go-live)

   PAPER TRADING MISSION:
   You are building an HONEST dataset that proves whether you have edge.
   Only enter setups where the evidence genuinely supports the trade.
   Skipping weak setups is CORRECT, not failure — a cycle with zero entries
   is a good cycle if there were no high-quality setups. Never manufacture
   trades to fill a quota or chase the goal.
   The goal ($155K+) is reached by DISCIPLINE and compounding, not by trading
   harder. Wanting a big number does not make a trade more likely to win. The
   market is indifferent to your goal. Trade each setup on its own merit.
   CRITICAL trajectory means be MORE selective, not more aggressive — a behind
   account cannot afford marginal trades.

4b. MULTI-TIMEFRAME CONFIRMATION: each asset now includes three trend labels:
   tf_15m_trend, tf_30m_trend, htf_1h_trend (up/down/sideways/unknown each).
   Your 5-minute signal should ALIGN with ALL THREE where possible.
   Confluence scoring:
   - All three align with your direction → strong confirmation, standard prob
   - Two of three align → acceptable, note the dissenter in reasoning
   - Only one or zero align → counter-trend in trending regimes; take ONLY with
     exceptional evidence and explicitly lower probability (subtract 0.05–0.10)
   VOLATILE REGIME EXCEPTION: when an asset's regime is VOLATILE, timeframe
   disagreement is EXPECTED and should NOT reduce your probability. Volatile
   markets are your highest-edge environment historically. In VOLATILE regime:
   - Trade momentum bursts and mean-reversion bounces
   - Use the 5-minute structure as your primary signal (not HTF alignment)
   - Set tighter TP targets (1.5-2.5% vs 3-4% for trending)
   - Wider SL is acceptable (volatile = bigger swings = more noise)
   - DO NOT skip a VOLATILE setup just because timeframes disagree
   Shorting when htf_1h_trend is "up" or longing when it is "down" requires
   explicit justification. When any trend is "sideways" or "unknown", be more
   conservative. Multi-timeframe alignment is your strongest structural edge —
   use it to filter marginal setups, not just to confirm good ones.

5. PROBABILITY SCORING — CHECKLIST (follow these steps EXACTLY for every asset):

   STEP 1 — Check each factor. Answer YES or NO using the market data:
   Y1: Trend?      Is "px" clearly above key EMAs (e20/e50/e200) for longs, below for shorts?
   Y2: Multi-TF?   Do 2+ of "t1h"/"t30m"/"t15m" agree with your direction?
   Y3: Volume?     Is "vol" above average for this asset?
   Y4: RSI OK?     Is "rsi" NOT overbought (>70 for longs), NOT oversold (<30 for shorts)?
   Y5: Structure?  Are "shi"/"slo" clear enough for SL/TP placement?
   Y6: Funding?    Does "fr" support you? Positive >0.0001 = short edge. Negative <-0.0001 = long edge.
   Y7: OrderBook?  Does "ob" show a wall >$50K behind your SL level?
   Y8: News?       Does "nsent" support your direction (bullish for longs, bearish for shorts)?
   Y9: OI?         Is "oi" high AND "fr" aligned with your direction?
   Y10: FearGreed? Is "fg" contrarian? Long when fg<25 OR short when fg>75?
   Y11: Scorecard? Is your win rate >50% on this direction+regime combo?
   Y12: Momentum?  Does "macd" show crossover or acceleration in your direction?

   STEP 2 — Check each PENALTY. Answer YES or NO:
   P1: Counter-trend?   Going against dominant "t1h"?
   P2: RSI extreme?     "rsi" >75 for longs or <25 for shorts?
   P3: Funding opposes?  Longing when "fr">0.0002 or shorting when "fr"<-0.0002?
   P4: FG crowded?      Longing when "fg">75 or shorting when "fg"<25?
   P5: Scorecard bad?   Win rate <35%? WARNING: this counts as TWO penalties.
   P6: Correlated?      3+ open trades in same group (BTC/ETH/SOL/XRP)?

   STEP 3 — Calculate probability (just count and multiply):
   probability = 0.50 + (YES_count x 0.05) - (PENALTY_count x 0.05)
   Remember: P5 = two penalties (-0.10 total, not -0.05).

   LOOKUP TABLE (use this to verify your math):
   YES=3  P=0 -> 0.65     YES=6  P=0 -> 0.80     YES=4  P=1 -> 0.65
   YES=4  P=0 -> 0.70     YES=7  P=0 -> 0.85     YES=5  P=1 -> 0.70
   YES=5  P=0 -> 0.75     YES=8  P=0 -> 0.90     YES=6  P=2 -> 0.70

   Below 0.65 = SKIP (do not output). Below 3 YES factors = SKIP.

   STEP 4 — Write reasoning for EVERY signal (MANDATORY):
   Format: "Y:T+MTF+V+RSI+S+FR=6 P:0 -> 0.80"
   Or: "Y:T+MTF+V=3 P:CT+FG=2 -> 0.55 SKIP"
   Show which factors are YES, which penalties apply, the counts, and the result.

   TRIGGER PRICE RULE: signals expire in 30 minutes. Crypto moves 0.1-0.4% in 30 min.
   Conviction triggers: within 0.1-0.3% of current price (pullback entries).
   Watchlist triggers: within 0.2-0.5% of current price (breakout entries).
   NEVER set triggers further than 0.5% — they will expire unfilled and waste a signal slot.

6. PENDING SIGNAL GENERATION (always produce alongside immediate trades):
   In ADDITION to your "trades" array, output a "pending_signals" array with
   conditional orders that fire between brain cycles when price hits triggers.

   TIER 1 — CONVICTION LADDERS (~2 per cycle):
   You have a genuine high-conviction call NOW. Set up to 6 laddered pending entries
   at different trigger prices to scale in at better levels.
   - Trigger type: "limit" or "stop" (your choice based on the setup)
   - Probability: your REAL conviction right now (same honest bar as trades)
   - SPLIT SIZING: divide the normal position size EQUALLY across all ladder entries.
     If you use N ladder entries, each gets 1/N of the position size (size_pct is
     the SPLIT amount per order, not the full size). 3 ladders = 1/3 each, 6 = 1/6 each.
     Both orders share a ladder_group string (e.g. "BTC-lad-1").
   - Trigger price MUST be away from current price (wait for pullback/breakout)

   TIER 2 — WATCHLIST BREAKOUT TRIGGERS (~5 per cycle):
   Asset is NOT tradeable now but BECOMES a trade if it breaks a key level.
   - Trigger type: ALWAYS "stop" (price must break PAST the level, confirming move)
   - Probability: FORWARD-LOOKING — "if it breaks $X, this becomes a 0.68 trade"
   - These are conditional setups: "not trading now, but if the level breaks, I'm in"
   - Generate up to 10 watchlist breakout signals per cycle

   RULES FOR ALL PENDING SIGNALS:
   - Same honest conviction bar as trades — do NOT lower standards for pending entries
   - ONE direction per asset per cycle (no long+short brackets on same symbol)
   - Up to 16 total: up to 6 conviction ladders + up to 10 watchlist breakouts
   - If no honest pending setups exist, output an empty array — never pad
   TRIGGER DISTANCE (critical — signals expire in 30 minutes):
   - Crypto moves ~0.1-0.4% in a typical 30-minute window
   - CONVICTION LIMITS (pullbacks): set 0.1-0.3% from current price, not further.
     A long limit at 0.15% below current = realistic pullback. 0.8% below = never fills.
   - WATCHLIST STOPS (breakouts): set just beyond the key level, within 0.2-0.5%
     of current price. The breakout trigger should be CLOSE to where price is
     consolidating, not at a distant level it would take hours to reach.
   - If a setup requires a trigger >0.5% away, it is unlikely to fill in 30 min —
     either move the trigger closer or skip that signal.
   - A signal that never triggers is wasted. Better to set a tight, reachable
     trigger that actually fills than a "perfect" level that expires every cycle.

7. STOP LOSS AND TAKE PROFIT PLACEMENT (use structure, not fixed percentages):
   Each asset includes swing_highs, swing_lows, and EMA levels from recent action.
   Place stop_loss at the STRUCTURAL level that invalidates your trade thesis:
   - LONGS: SL just below the nearest swing low or support level. If price
     breaks that low, your long thesis is wrong. Example: entry $1.80, nearest
     swing low at $1.775 — set SL at $1.772 (just below the level).
   - SHORTS: SL just above the nearest swing high or resistance level. If price
     breaks that high, your short thesis is wrong.
   - DO NOT use a fixed percentage from entry (like always 2% below). Markets
     move based on structure, not your entry price. A trade near tight structure
     gets a tight SL (0.8%). A trade with wide structure gets a wide SL (3%).
   - If no clear swing level exists, use the nearest EMA (ema50 or ema200).
   - ORDER BOOK: if "ob" shows a large bid wall (>$50K) near SL (longs), place SL
     just below it. For shorts, use ask wall above SL. Supplements swing levels.
   - SL BAND: code enforces 1.5% minimum, 2.5% maximum from entry. Your structural SL will be
     clamped to this range. Set SL at the real structural level — if it falls within 1.5-2.5%,
     it's used as-is. Tighter than 1.5% gets widened; wider than 2.5% gets clamped.
   - R:R must be at least 1.25:1 (code gate). Aim for 1.5:1 or better — higher R:R means the code gate and fill-side gate (1.1:1) will both pass it easily.
     At 1.5% SL → TP needs 2.25%. At 2% SL → TP needs 3%. At 2.5% SL → TP needs 3.75%.
   Place take_profit at the next structural target:
   - LONGS: TP at the next swing high or resistance above entry
   - SHORTS: TP at the next swing low or support below entry
   - Set TP at least 1.5× the SL distance (ideal). The code gate accepts down to 1.25:1. Below 1.25:1 is rejected.
   This applies to BOTH immediate trades AND pending signals.

VOLATILE PAIR TP RULE
For high-volatility pairs (HYPE, TAO, SUI, INJ, WIF, BONK, PEPE, FLOKI, JUP, ONDO, PENDLE, RENDER, APT, TIA, NEAR): use the 2nd or 3rd swing level for TP, not the nearest one.
These pairs move 3-8% daily — a 1.5% TP wastes their range. Target 2.5%-4.0% TP on volatile pairs.
SL rules are unchanged — use the structural SL within the 1.5%-2.5% band.

OUTPUT FORMAT (strict):
Respond with ONE valid JSON object and nothing else — no prose, no markdown fences.
Use DOUBLE QUOTES (") for every key and every string value. Do NOT use single
quotes — this is JSON, not a Python dict. Use true/false/null (lowercase), not
Python's True/False/None."""

_USER_PROMPT_TEMPLATE = """PAPER TRADING MODE — objective is maximum signal volume for data collection.
Output ALL valid signals >= 0.65 probability. Do NOT self-filter based on trajectory.
Do NOT restrict to "high-conviction only." Every 0.65+ signal is a valid trade.

Trajectory reference (informational only — do NOT restrict signal output based on this):
{trajectory_json}

SIZING STATE:
{sizing_json}

MARKET DATA:
{market_context_json}

OPEN POSITIONS:
{open_positions_json}

YOUR OWN TRACK RECORD (last 30 closed — review this and CORRECT your mistakes):
{scorecard_json}
Use this to self-correct THIS cycle. If your longs lose, be far more skeptical of
longs. If your stated 0.70 calls win much less than 70%, you are overconfident —
lower your probabilities. If you lose in a regime, avoid it. Your past results are
your most honest feedback. Do not repeat patterns that lose.

RECENT PERFORMANCE (last 5 closed):
{recent_trades_json}

CURRENT OPERATOR SETTINGS:
{operator_controls_json}

PENDING SIGNAL OUTCOMES (last cycle):
{signal_outcomes_json}
Review which of your pending signals FIRED (now open positions) and which EXPIRED
un-triggered. If many expired, your trigger levels were mis-placed -- adjust this
cycle. If none fired, consider whether your triggers are too far from current price.

Respond with this exact JSON schema:
{{
  "cycle_assessment": "one sentence",
  "trajectory_note": "one sentence on goal progress",
  "regime_by_asset": {{"SYMBOL": "REGIME"}},
  "trades": [
    {{
      "symbol": "SOLUSD",
      "direction": "short",
      "entry_price": 72.50,
      "stop_loss": 74.05,
      "take_profit": 68.20,
      "size_pct": 2.5,
      "probability": 0.71,
      "reasoning": "Two sentences max."
    }}
  ],
  "pending_signals": [
    {{
      "symbol": "BTCUSD",
      "direction": "long",
      "tier": "conviction",
      "trigger_type": "limit",
      "trigger_price": 62500.00,
      "stop_loss": 61800.00,
      "take_profit": 64200.00,
      "size_pct": 1.25,
      "probability": 0.72,
      "ladder_group": "BTC-lad-1",
      "reasoning": "Pullback to EMA20 support"
    }}
  ],
  "skipped": {{"SYMBOL": "reason"}},
  "sizing_note": "Kelly applied, vol scalar X"
}}"""


async def _call_claude(
    trajectory: dict,
    sizing_state: dict,
    market_context: dict,
    open_positions: list,
    recent_trades: list,
    scorecard: dict,
    controls: dict,
    signal_outcomes: str = "",
) -> tuple[dict | None, float]:
    """Call Claude with full brain context. Returns (parsed_json, cost_usd)."""
    global _daily_spend

    _check_reset()

    if _daily_spend >= config.CLAUDE_DAILY_BUDGET_USD:
        log.warning("BRAIN: daily budget exhausted ($%.2f) — skipping cycle", _daily_spend)
        return None, 0.0

    user_message = _USER_PROMPT_TEMPLATE.format(
        trajectory_json=json.dumps(trajectory, indent=2),
        sizing_json=json.dumps({
            "kelly_fraction": sizing_state.get("kelly_fraction", config.KELLY_FRACTION_DEFAULT),
            "win_rate": sizing_state.get("win_rate", 0),
            "total_trades": sizing_state.get("total_trades", 0),
            "sizing_mode": sizing_state.get("sizing_mode", "fractional_kelly"),
        }, indent=2),
        market_context_json=json.dumps(market_context, indent=2),
        open_positions_json=json.dumps(open_positions, indent=2),
        recent_trades_json=json.dumps(recent_trades, indent=2),
        scorecard_json=json.dumps(scorecard, indent=2),
        operator_controls_json=json.dumps({
            "max_open_trades": controls.get("max_open_trades", config.MAX_OPEN_TRADES),
            "max_position_pct": controls.get("max_position_pct", config.MAX_TRADE_SIZE_PCT),
            "crypto_enabled": controls.get("crypto_enabled", True),
            "kalshi_enabled": controls.get("kalshi_enabled", False),
            "stocks_enabled": controls.get("stocks_enabled", False),
        }, indent=2),
        signal_outcomes_json=signal_outcomes,
    )

    try:
        resp = await _client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=16384,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        cost = (
            resp.usage.input_tokens * config.CLAUDE_INPUT_COST_PER_TOKEN
            + resp.usage.output_tokens * config.CLAUDE_OUTPUT_COST_PER_TOKEN
        )
        _daily_spend += cost

        log.info(
            "BRAIN CLAUDE CALL — in=%d out=%d cost=$%.4f total_today=$%.4f",
            resp.usage.input_tokens, resp.usage.output_tokens, cost, _daily_spend,
        )

        if _daily_spend >= config.CLAUDE_BUDGET_ALERT_AT:
            await alert_manager.alert_budget_warning(_daily_spend, config.CLAUDE_DAILY_BUDGET_USD)

        parsed = _parse_brain_response(raw)
        return parsed, cost

    except Exception as exc:  # noqa: BLE001
        log.error("BRAIN: Claude call failed: %s", exc)
        return None, 0.0


def _parse_brain_response(text: str) -> dict:
    """Parse the brain JSON response, tolerating single-quoted (Python-dict) output.

    The prompt asks for strict JSON, but Claude occasionally returns Python-dict
    style (single quotes). json.loads() rejects that, so fall back to
    ast.literal_eval() (safe — literals only, no code execution).
    """
    text = re.sub(r"```(?:json)?\n?", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in brain response")
    blob = match.group()

    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        log.warning("BRAIN: json.loads failed — retrying with ast.literal_eval (single-quoted response)")

    try:
        result = ast.literal_eval(blob)
    except (ValueError, SyntaxError):
        # literal_eval can't read JSON true/false/null — map to Python and retry.
        py = re.sub(r"\btrue\b", "True", blob)
        py = re.sub(r"\bfalse\b", "False", py)
        py = re.sub(r"\bnull\b", "None", py)
        result = ast.literal_eval(py)

    if not isinstance(result, dict):
        raise ValueError("Brain response did not parse to a dict")
    return result


# ── STEP 4: PARSE AND EXECUTE ─────────────────────────────────────────────

_REQUIRED_TRADE_FIELDS = {"symbol", "direction", "entry_price", "stop_loss", "take_profit", "size_pct", "probability", "reasoning"}


def _count_correlated_open(symbol: str, open_trades: list) -> int:
    """Count how many trades in the same correlation group are currently open."""
    for group in config.CORRELATED_PAIRS:
        if symbol in group:
            return sum(1 for t in open_trades if t.get("symbol") in group)
    return 0


async def _execute_brain_trades(
    brain_json: dict,
    scan_results: list,
    trajectory: dict,
    sizing_state: dict,
    db,
    controls: dict,
    cost_usd: float,
) -> int:
    """Validate, gate-check, and execute each trade from Claude's JSON. Returns count opened."""
    trades = brain_json.get("trades") or []
    regime_by_asset = brain_json.get("regime_by_asset") or {}
    trajectory_status = trajectory.get("trajectory_status", "ON_TRACK")
    equity = trajectory.get("equity", config.STARTING_EQUITY)
    entries_this_cycle = 0

    # Index scan results by symbol for quick lookup.
    scan_by_symbol = {sr.symbol: sr for sr in scan_results}

    open_trades = await db.get_open_trades()
    max_open = int(controls.get("max_open_trades") or config.MAX_OPEN_TRADES)

    for trade_spec in trades:
        if entries_this_cycle >= config.MAX_ENTRIES_PER_CYCLE:
            log.info("BRAIN: max entries per cycle (%d) reached", config.MAX_ENTRIES_PER_CYCLE)
            break

        # Enforce the operator's live max_open_trades cap. The gate checks the
        # static config value; this honors a dashboard override that lowers it.
        if len(open_trades) >= max_open:
            log.info("BRAIN: operator max_open_trades (%d) reached — no more entries", max_open)
            break

        # Validate all required fields.
        missing = _REQUIRED_TRADE_FIELDS - set(trade_spec.keys())
        if missing:
            log.warning("BRAIN: skipping trade — missing fields: %s", missing)
            continue

        symbol = trade_spec["symbol"]
        direction = trade_spec["direction"]
        probability = float(trade_spec.get("probability", 0))
        if probability == 0.0:
            _reasoning = trade_spec.get("reasoning") or trade_spec.get("rationale") or ""
            recovered = _extract_prob_from_reasoning(_reasoning)
            if recovered > 0:
                probability = recovered
                log.info("BRAIN: recovered prob=%.2f from reasoning for %s", probability, trade_spec.get("symbol", "?"))
        size_pct = float(trade_spec.get("size_pct", 0))
        claude_entry = float(trade_spec.get("entry_price", 0))
        stop_loss = float(trade_spec.get("stop_loss", 0))
        take_profit = float(trade_spec.get("take_profit", 0))
        reasoning = trade_spec.get("reasoning", "")
        regime = regime_by_asset.get(symbol, "UNKNOWN")

        # Anchor the booked entry to the LIVE scanned price (paper market fill),
        # not Claude's quoted level which may be stale or a price never traded.
        # Fall back to Claude's entry only when no live price is available.
        sr = scan_by_symbol.get(symbol)
        live_price = float(getattr(sr, "current_price", 0) or 0) if sr else 0.0
        entry_price = live_price or claude_entry
        if entry_price <= 0:
            log.info("BRAIN: skip %s — no usable entry price", symbol)
            continue

        # Enforce minimum SL distance (crypto noise floor).
        min_sl_pct = 0.02  # 2% minimum from entry
        if direction in ("long", "yes"):
            min_sl = entry_price * (1 - min_sl_pct)
            if stop_loss > min_sl:
                stop_loss = min_sl
        elif direction in ("short", "no"):
            min_sl = entry_price * (1 + min_sl_pct)
            if stop_loss < min_sl:
                stop_loss = min_sl

        # If price has already run past Claude's stop, the entry is invalid (it
        # would stop out on the first monitor tick) — skip rather than book it.
        if stop_loss > 0:
            long_like = direction in ("long", "yes")
            if long_like and entry_price <= stop_loss:
                log.info("BRAIN: skip %s long — live %.6f already <= SL %.6f", symbol, entry_price, stop_loss)
                continue
            if not long_like and entry_price >= stop_loss:
                log.info("BRAIN: skip %s short — live %.6f already >= SL %.6f", symbol, entry_price, stop_loss)
                continue

        # Operator controls: double-check before each trade.
        if controls.get("kill_switch"):
            log.info("BRAIN: kill_switch active — halting execution")
            break
        if controls.get("pause_entries"):
            log.info("BRAIN: pause_entries active — no new trades")
            break

        # Validate probability floor.
        if probability < config.MIN_CONFIDENCE:
            log.info("BRAIN: skip %s — prob %.2f below floor %.2f", symbol, probability, config.MIN_CONFIDENCE)
            continue

        # Ensure R:R >= 1.25 by widening TP instead of rejecting (matches pending path).
        if take_profit > 0 and stop_loss > 0 and entry_price > 0:
            _ir = abs(entry_price - stop_loss)
            _iw = abs(take_profit - entry_price)
            if _ir > 0 and (_iw / _ir) < 1.25:
                _ntp = _widen_tp(entry_price, stop_loss, direction, target_rr=1.5)
                log.info("BRAIN: widen TP %s R:R=%.2f→1.50 (tp %.6f→%.6f)", symbol, _iw / _ir, take_profit, _ntp)
                take_profit = _ntp

        # Validate size bounds.
        max_pct = config.MAX_TRADE_SIZE_PCT_BEHIND if trajectory_status in ("BEHIND", "CRITICAL") else config.MAX_TRADE_SIZE_PCT
        if size_pct / 100.0 > max_pct:
            log.warning("BRAIN: clamping %s size_pct %.1f → %.1f%%", symbol, size_pct, max_pct * 100)
            size_pct = max_pct * 100.0
        if size_pct / 100.0 < config.MIN_TRADE_SIZE_PCT:
            size_pct = config.MIN_TRADE_SIZE_PCT * 100.0

        # Correlated pair check — Claude should catch this but verify.
        corr_count = _count_correlated_open(symbol, open_trades)
        if corr_count >= 3:
            size_pct *= 0.5
            # Re-apply the 0.5% floor — the penalty must not push size below the minimum.
            size_pct = max(size_pct, config.MIN_TRADE_SIZE_PCT * 100.0)
            log.info("BRAIN: %s correlated penalty — 3+ in group — size halved to %.1f%%", symbol, size_pct)

        size_usd = (size_pct / 100.0) * equity

        # Paper-phase minimum: 2.5% of equity or $100, whichever is larger
        min_size = max(100.0, equity * 0.025)
        if size_usd < min_size:
            log.info("SIZE FLOOR: $%.2f → $%.2f (min 2.5%% of equity)", size_usd, min_size)
            size_usd = min_size

        # Build a Signal for gate.check_gate (gate is unchanged). sr was resolved above.
        if sr is None:
            sr = ScanResult(market="crypto", symbol=symbol, current_price=entry_price, scan_time="")
        signal = Signal(
            market="crypto",
            symbol=symbol,
            direction=direction,
            confidence=probability,
            technical_score=probability * 100,
            news_sentiment="neutral",
            news_headline="",
            news_boost=0.0,
            macro_blocked=False,
            block_reason="",
            scan_result=sr,
        )

        gate_result = await gate.check_gate(signal, db)
        if not gate_result.passed:
            log.info("BRAIN: gate reject %s — %s", symbol, gate_result.reason)
            continue

        # Block opposite-direction trades on the same symbol (same-direction stacking OK).
        sym_open = [t for t in open_trades if t.get("symbol") == symbol]
        if sym_open and any(t.get("direction") != direction for t in sym_open):
            log.info("BRAIN: skip %s %s — open %s position exists on %s",
                     symbol, direction, sym_open[0].get("direction"), symbol)
            continue

        # Build trade record with all columns including new brain v2 fields.
        kelly_fraction = float(sizing_state.get("kelly_fraction") or config.KELLY_FRACTION_DEFAULT)
        trade_record = {
            "bot_name": config.BOT_NAME,
            "market": "crypto",
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": None,
            "size_usd": round(size_usd, 2),
            "confidence": probability,
            "pnl": 0,
            "status": "open",
            "close_reason": None,
            "paper_mode": config.PAPER_MODE,
            "sl": stop_loss,
            "tp": take_profit,
            # New brain v2 columns:
            "instrument_type": "crypto",
            "market_regime": regime,
            "claude_reasoning": reasoning[:1000],
            "predicted_prob": probability,
            "kelly_fraction": kelly_fraction,
            "trajectory_status": trajectory_status,
        }

        trade_id = await db.log_trade(trade_record)
        if not trade_id:
            log.error("BRAIN: db.log_trade failed for %s", symbol)
            continue

        # Log prediction for calibration tracking.
        await db.log_prediction({
            "trade_id": trade_id,
            "symbol": symbol,
            "direction": direction,
            "stated_prob": probability,
            "outcome": None,
            "actual_win": None,
        })

        # Large trade alert.
        if size_usd > equity * 0.04:
            await alert_manager.alert_large_trade(symbol, size_usd, equity)

        entries_this_cycle += 1
        open_trades.append(trade_record)  # keep local list current for correlation checks

        log.info(
            "TRADE OPEN %s %s size=$%.2f entry=%.5f sl=%.5f tp=%.5f prob=%.2f regime=%s (%s)",
            symbol, direction, size_usd, entry_price, stop_loss, take_profit,
            probability, regime, "PAPER" if config.PAPER_MODE else "LIVE",
        )

    # Log skipped symbols.
    skipped = brain_json.get("skipped") or {}
    for sym, reason in skipped.items():
        log.info("BRAIN SKIP %s — %s", sym, reason)

    return entries_this_cycle


# ── STEP 5: SIZING STATE ──────────────────────────────────────────────────

async def _update_sizing_state(db, trajectory_status: str):
    """Recalculate Kelly fraction and sizing mode from the last 30 closed trades
    (rolling window — aligned with worker._refresh_sizing_state)."""
    try:
        trades = await db.get_closed_trades_last_n(30)
        total = len(trades)
        if total == 0:
            return

        winners = [t for t in trades if (t.get("pnl") or 0) > 0]
        losers = [t for t in trades if (t.get("pnl") or 0) <= 0]

        win_rate = len(winners) / total
        loss_rate = 1 - win_rate

        def _avg(lst, field):
            vals = [abs(float(t.get(field) or 0)) for t in lst if t.get(field)]
            return sum(vals) / len(vals) if vals else 0.0

        avg_win = _avg(winners, "pnl")
        avg_loss = _avg(losers, "pnl")
        worst_loss = max((abs(float(t.get("pnl") or 0)) for t in losers), default=0.0)

        kelly_raw = 0.0
        if avg_win > 0:
            kelly_raw = (win_rate * avg_win - loss_rate * avg_loss) / avg_win
        kelly_fraction = max(0.01, min(kelly_raw * config.KELLY_FRACTION_DEFAULT, 0.50))

        sizing_mode = "fractional_kelly"
        optimal_f = None

        if total >= 20:
            sizing_mode = "optimal_f"
            if avg_loss > 0 and avg_win > 0:
                # Ralph Vince optimal-f approximation.
                optimal_f = round(
                    (win_rate * avg_win - loss_rate * avg_loss) / avg_win, 4
                )
                kelly_fraction = max(0.01, min((optimal_f or kelly_fraction) * 0.5, 0.50))

        updates = {
            "total_trades": total,
            "win_rate": round(win_rate, 4),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "worst_loss": round(worst_loss, 4),
            "kelly_fraction": round(kelly_fraction, 4),
            "optimal_f": optimal_f,
            "sizing_mode": sizing_mode,
            "trajectory": trajectory_status,
        }
        await db.update_sizing_state(updates)

        log.info(
            "SIZING UPDATE — trades=%d win_rate=%.0f%% kelly=%.3f mode=%s",
            total, win_rate * 100, kelly_fraction, sizing_mode,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("_update_sizing_state failed: %s", exc)


# ── MAIN CYCLE ────────────────────────────────────────────────────────────

async def run_brain_cycle(scan_results: list, news_by_symbol: dict, db, controls: dict):
    """Execute one full brain cycle. Called by worker every 55 minutes."""
    global _last_brain_cycle_time, _consecutive_missed_cycles
    _check_reset()

    log.info("═══ BRAIN CYCLE START ═══ spend_today=$%.4f", _daily_spend)

    # Step 1: Goal pulse.
    trajectory = await _run_goal_pulse(db)
    trajectory_status = trajectory.get("trajectory_status", "ON_TRACK")
    equity = trajectory.get("equity", config.STARTING_EQUITY)

    # Abort entries if equity could not be read — never size against a fabricated value.
    if not trajectory.get("equity_available", True):
        log.error("BRAIN: equity unavailable this cycle — skipping all entries")
        return

    # Market hours gate (CDT).
    now_cdt = datetime.now(CDT)
    hour = now_cdt.hour
    in_prime_session = (8 <= hour < 12) or (20 <= hour < 24)
    in_dead_session = 2 <= hour < 6
    if in_dead_session and not config.PAPER_MODE and trajectory_status != "CRITICAL":
        log.info("BRAIN: dead session hour=%d CDT — skipping entries (not CRITICAL)", hour)
        await _update_sizing_state(db, trajectory_status)
        return

    if not scan_results:
        log.warning("BRAIN: no scan results — skipping Claude call")
        return

    # Step 2: Market context.
    market_context = _build_market_context(scan_results, news_by_symbol or {})

    # Persist latest news so the terminal can display a fresh news feed.
    try:
        await db.save_latest_news(news_by_symbol or {})
    except Exception as exc:  # noqa: BLE001
        log.error("save_latest_news call failed: %s", exc)

    # Pull supporting data.
    open_trades = await db.get_open_trades()
    recent_trades = await db.get_closed_trades_last_n(5)
    scorecard_trades = await db.get_closed_trades_last_n(30)
    sizing_state = await db.get_sizing_state()

    open_positions_ctx = _build_open_positions_context(open_trades)
    recent_trades_ctx = _build_recent_trades_context(recent_trades)
    scorecard = _build_scorecard(scorecard_trades)

    # Budget pre-check: a budget-capped cycle is HEALTHY, not a missed/crashed
    # cycle. Short-circuit here so the 'no Claude response' path below only ever
    # represents a real API failure (which is what the bot-silent alert means).
    if get_daily_spend() >= config.CLAUDE_DAILY_BUDGET_USD:
        log.warning(
            "BRAIN: daily budget $%.2f exhausted — skipping Claude this cycle (healthy)",
            get_daily_spend(),
        )
        await _update_sizing_state(db, trajectory_status)
        return

    # Get pending signal outcomes for brain feedback.
    signal_outcomes = await db.get_recent_signal_outcomes()
    signal_outcomes_str = json.dumps(signal_outcomes, indent=2) if signal_outcomes else "[]"

    # Step 3: Claude brain call.
    brain_json, cost_usd = await _call_claude(
        trajectory=trajectory,
        sizing_state=sizing_state,
        market_context=market_context,
        open_positions=open_positions_ctx,
        recent_trades=recent_trades_ctx,
        scorecard=scorecard,
        controls=controls,
        signal_outcomes=signal_outcomes_str,
    )
    await db.persist_spend_today(get_daily_spend())

    if brain_json is None:
        log.warning("BRAIN: no valid response from Claude this cycle")
        _consecutive_missed_cycles += 1
        if _consecutive_missed_cycles >= 2:
            await alert_manager.alert_bot_silent(_consecutive_missed_cycles)
        return

    _consecutive_missed_cycles = 0

    log.info(
        "BRAIN RESPONSE — assessment: %s | trajectory: %s",
        brain_json.get("cycle_assessment", ""),
        brain_json.get("trajectory_note", ""),
    )

    # Log all detected regimes (powers the dashboard regime chart).
    regime_by_asset = brain_json.get("regime_by_asset") or {}
    for sym, regime in regime_by_asset.items():
        asset_ctx = market_context.get(sym, {})
        try:
            await db.log_regime({
                "market": "crypto",
                "regime": regime,
                "reasoning": brain_json.get("cycle_assessment", ""),
                "confidence": None,
                "vol_30d": asset_ctx.get("vol"),
                "trajectory": trajectory_status,
            })
        except Exception as exc:  # noqa: BLE001
            log.error("regime_log for %s failed: %s", sym, exc)

    # Step 4: Parse and execute trades (or save pending signals).
    effective_pending = controls.get(
        "pending_signals_enabled", config.PENDING_SIGNALS_ENABLED
    )
    if effective_pending:
        # Pending mode: save signals to DB; monitor fires them on triggers.
        pending = brain_json.get("pending_signals") or []
        regime_by_asset = brain_json.get("regime_by_asset") or {}
        # Filter out signals below MIN_CONFIDENCE.
        filtered = []
        for sig in pending:
            sig["regime"] = regime_by_asset.get(sig.get("symbol"), "UNKNOWN")
            prob = float(sig.get("probability") or 0)
            if prob == 0.0:
                _reasoning = sig.get("reasoning") or sig.get("rationale") or ""
                recovered = _extract_prob_from_reasoning(_reasoning)
                if recovered > 0:
                    prob = recovered
                    sig["probability"] = prob
                    log.info("PENDING: recovered prob=%.2f from reasoning for %s", prob, sig.get("symbol", "?"))
            if prob < config.MIN_CONFIDENCE:
                log.info("PENDING: skip %s prob=%.2f < floor %.2f",
                         sig.get("symbol"), prob, config.MIN_CONFIDENCE)
                continue
            # Enforce minimum 1.25:1 reward-to-risk ratio — widen TP instead of rejecting.
            _trig = float(sig.get("trigger_price") or 0)
            _sl = float(sig.get("stop_loss") or 0)
            _tp = float(sig.get("take_profit") or 0)
            if _trig > 0 and _sl > 0 and _tp > 0:
                _dir = sig.get("direction", "")
                if _dir in ("long", "yes"):
                    _risk = abs(_trig - _sl)
                    _reward = abs(_tp - _trig)
                else:
                    _risk = abs(_sl - _trig)
                    _reward = abs(_trig - _tp)
                if _risk > 0 and (_reward / _risk) < 1.25:
                    _new_tp = _widen_tp(_trig, _sl, _dir, target_rr=1.5)
                    log.info("PENDING: widen TP %s R:R=%.2f→1.50 (tp %.6f→%.6f)",
                             sig.get("symbol", "?"), _reward / _risk, _tp, _new_tp)
                    sig["take_profit"] = _new_tp
                    _tp = _new_tp
            filtered.append(sig)
        # Validate symbols against scanned pairs (drop Haiku typos like RENDERU$D).
        _valid_symbols = {sr.symbol for sr in scan_results}
        _validated = []
        for _s in filtered:
            if _s.get("symbol") in _valid_symbols:
                _validated.append(_s)
            else:
                log.warning("PENDING: drop %s — symbol not in scanned pairs (possible Haiku typo)",
                            _s.get("symbol", "?"))
        filtered = _validated
        await db.save_pending_signals(filtered)
        entries = 0
        log.info("BRAIN: pending mode ON -- saved %d signals (%d filtered), skipped immediate", len(filtered), len(pending) - len(filtered))
    else:
        entries = await _execute_brain_trades(
            brain_json=brain_json,
            scan_results=scan_results,
            trajectory=trajectory,
            sizing_state=sizing_state,
            db=db,
            controls=controls,
            cost_usd=cost_usd,
        )

    # Step 5: Update sizing state.
    await _update_sizing_state(db, trajectory_status)

    _last_brain_cycle_time = datetime.now(timezone.utc).timestamp()

    # Snapshot equity for the terminal chart.
    try:
        total_pnl = await db.get_total_realized_pnl()
        real_equity = config.STARTING_EQUITY + total_pnl
        await db.supabase.table("equity_snapshots").insert({
            "equity": round(real_equity, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        log.error("equity snapshot failed: %s", exc)

    log.info("═══ BRAIN CYCLE END ═══ entries=%d spend_today=$%.4f", entries, _daily_spend)


# ── BACKWARD COMPATIBILITY (executor._claude_override) ───────────────────

async def validate_signal(signal: Signal, db, portfolio_summary: dict) -> BrainResult:
    """Hold-or-exit query for executor's adverse-move handler. Not a full brain cycle."""
    global _daily_spend
    _check_reset()

    if _daily_spend >= config.CLAUDE_DAILY_BUDGET_USD:
        return BrainResult("ENTER", signal.confidence, "Budget exhausted — default hold", 0.0, True)

    prompt = f"""RAID adverse move check. Hold or exit this position?

Symbol: {signal.symbol}
Direction: {signal.direction}
Current confidence: {signal.confidence:.0%}
Open trades: {portfolio_summary.get('open_count', 0)}
Consecutive losses: {portfolio_summary.get('consecutive_losses', 0)}
Daily PnL: ${portfolio_summary.get('daily_pnl', 0):.2f}

The position has moved >2% against us. Should we HOLD and let SL/TP manage it,
or EXIT now to cut the loss?

Respond in exactly this format:
DECISION: ENTER or SKIP
CONFIDENCE: 0.XX
REASONING: one sentence"""

    try:
        resp = await _client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        cost = (
            resp.usage.input_tokens * config.CLAUDE_INPUT_COST_PER_TOKEN
            + resp.usage.output_tokens * config.CLAUDE_OUTPUT_COST_PER_TOKEN
        )
        _daily_spend += cost

        decision = "ENTER"
        confidence = signal.confidence
        reasoning = ""
        for line in text.splitlines():
            line = line.strip()
            if line.upper().startswith("DECISION:"):
                val = line.split(":", 1)[1].strip().upper()
                decision = "ENTER" if "ENTER" in val else "SKIP"
            elif line.upper().startswith("CONFIDENCE:"):
                m = re.search(r"[0-9]*\.?[0-9]+", line.split(":", 1)[1])
                if m:
                    try:
                        confidence = float(m.group())
                    except ValueError:
                        pass
            elif line.upper().startswith("REASONING:"):
                reasoning = line.split(":", 1)[1].strip()

        return BrainResult(decision, confidence, reasoning, cost)
    except Exception as exc:  # noqa: BLE001
        log.error("validate_signal failed: %s", exc)
        return BrainResult("ENTER", signal.confidence, f"AI error: {exc}", 0.0)


# ── BACKWARD COMPATIBILITY (worker calls this weekly) ─────────────────────

async def run_weekly_learning(db):
    """Superseded by Kelly/sizing_state in brain v2. Updates sizing_state instead."""
    log.info("run_weekly_learning: v2 — delegating to sizing_state update")
    try:
        controls = await db.get_operator_controls()
        trajectory = await _run_goal_pulse(db)
        await _update_sizing_state(db, trajectory.get("trajectory_status", "ON_TRACK"))
    except Exception as exc:  # noqa: BLE001
        log.error("run_weekly_learning failed: %s", exc)
