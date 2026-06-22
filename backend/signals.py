"""RAID signals — turn scan results into scored, directional Signal objects."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import config
import scanner
from scanner import ScanResult

log = logging.getLogger("raid.signals")


@dataclass
class Signal:
    """A scored, directional trade candidate ready for the gate and the brain."""

    market: str
    symbol: str
    direction: str  # long/short/yes/no
    confidence: float  # 0.0 to 1.0
    technical_score: float
    news_sentiment: str
    news_headline: str
    news_boost: float
    macro_blocked: bool
    block_reason: str
    scan_result: ScanResult


def _ema(values, period):
    """Return the exponential moving average series for the given period."""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    out = [ema]
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
        out.append(ema)
    return out


def _rsi(closes, period):
    """Return the latest RSI value (0-100) using a Wilder-style average."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0)
        loss = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _macd(closes):
    """Return (macd_line, signal_line) as series, or ([], []) if insufficient data."""
    if len(closes) < config.MACD_SLOW + config.MACD_SIGNAL:
        return [], []
    fast = _ema(closes, config.MACD_FAST)
    slow = _ema(closes, config.MACD_SLOW)
    offset = len(fast) - len(slow)
    fast = fast[offset:]
    macd_line = [f - s for f, s in zip(fast, slow)]
    signal_line = _ema(macd_line, config.MACD_SIGNAL)
    align = len(macd_line) - len(signal_line)
    return macd_line[align:], signal_line


def _macd_cross(closes):
    """Return 'bullish'/'bearish'/'none' based on the latest MACD/signal crossover."""
    macd_line, signal_line = _macd(closes)
    if len(macd_line) < 2 or len(signal_line) < 2:
        return "none"
    prev_diff = macd_line[-2] - signal_line[-2]
    curr_diff = macd_line[-1] - signal_line[-1]
    if prev_diff <= 0 < curr_diff:
        return "bullish"
    if prev_diff >= 0 > curr_diff:
        return "bearish"
    return "none"


def _ema_last(values, period):
    """Return the final EMA value for a period, or None if insufficient data."""
    series = _ema(values, period)
    return series[-1] if series else None


def detect_direction(ohlcv):
    """Return 'long'/'short' from EMA-stack trend, MACD state, then crossover; None if unclear."""
    try:
        closes = [c[4] for c in ohlcv]
        if len(closes) < config.RSI_PERIOD + 1:
            return None
        price = closes[-1]
        ema20 = _ema_last(closes, config.EMA_FAST)
        ema50 = _ema_last(closes, config.EMA_MID)
        ema200 = _ema_last(closes, config.EMA_SLOW)
        macd_line, signal_line = _macd(closes)
        macd_bull = bool(macd_line and signal_line and macd_line[-1] > signal_line[-1])
        macd_bear = bool(macd_line and signal_line and macd_line[-1] < signal_line[-1])

        # Primary: full EMA-stack trend alignment — the trend-following core.
        if ema20 and ema50 and ema200:
            if price > ema20 > ema50 > ema200:
                return "long"
            if price < ema20 < ema50 < ema200:
                return "short"

        # Secondary: short/medium EMA alignment confirmed by MACD state.
        if ema20 and ema50:
            if price < ema20 < ema50 and macd_bear:
                return "short"
            if price > ema20 > ema50 and macd_bull:
                return "long"

        # Fallback: a fresh MACD crossover.
        cross = _macd_cross(closes)
        if cross == "bullish":
            return "long"
        if cross == "bearish":
            return "short"
        return None
    except Exception as exc:  # noqa: BLE001
        log.error("detect_direction failed: %s", exc)
        return None


def score_technical(ohlcv, direction):
    """Return a 0-100 score for how strongly the indicators confirm `direction`."""
    try:
        closes = [c[4] for c in ohlcv]
        volumes = [c[5] for c in ohlcv]
        if len(closes) < config.RSI_PERIOD + 1 or direction not in ("long", "short"):
            return 0.0
        bullish = direction == "long"
        score = 0.0

        price = closes[-1]
        ema20 = _ema_last(closes, config.EMA_FAST)
        ema50 = _ema_last(closes, config.EMA_MID)
        ema200 = _ema_last(closes, config.EMA_SLOW)

        # EMA trend confirmation: full stack is strongest, short/medium is mixed.
        if ema20 and ema50 and ema200 and (
            (bullish and price > ema20 > ema50 > ema200)
            or (not bullish and price < ema20 < ema50 < ema200)
        ):
            score += config.TS_EMA_STRONG
        elif ema20 and ema50 and (
            (bullish and price > ema20 > ema50)
            or (not bullish and price < ema20 < ema50)
        ):
            score += config.TS_EMA_MIXED

        # MACD state aligned with the trade direction (tie credits the detected trend).
        macd_line, signal_line = _macd(closes)
        if macd_line and signal_line:
            diff = macd_line[-1] - signal_line[-1]
            if (bullish and diff >= 0) or (not bullish and diff <= 0):
                score += config.TS_MACD_WEIGHT

        # RSI on the correct side of 50 for the trend.
        rsi = _rsi(closes, config.RSI_PERIOD)
        if (bullish and rsi > 50) or (not bullish and rsi < 50):
            score += config.TS_RSI_WEIGHT

        # Volume confirmation.
        if len(volumes) > 1:
            avg_vol = sum(volumes[:-1]) / len(volumes[:-1])
            if avg_vol > 0 and volumes[-1] > config.VOLUME_CONFIRM_MULT * avg_vol:
                score += config.TS_VOLUME_WEIGHT

        return min(score, 100.0)
    except Exception as exc:  # noqa: BLE001
        log.error("score_technical failed: %s", exc)
        return 0.0


def score_kalshi(scan_result):
    """Return (confidence, direction) for a Kalshi market; direction is 'yes'/'no'/None."""
    try:
        yes_price = scan_result.yes_price
        if yes_price is None:
            return 0.0, None
        if config.KALSHI_SKIP_LOW < yes_price < config.KALSHI_SKIP_HIGH:
            return 0.0, None

        if yes_price < config.KALSHI_YES_LOW:
            direction = "yes"
            confidence = config.KALSHI_BASE_CONF
        elif yes_price > config.KALSHI_YES_HIGH:
            direction = "no"
            confidence = config.KALSHI_BASE_CONF
        else:
            return 0.0, None

        if (scan_result.volume_24h or 0) > config.KALSHI_VOLUME_BOOST_THRESHOLD:
            confidence += config.KALSHI_VOLUME_BOOST

        if scan_result.resolution_time:
            try:
                res_dt = datetime.fromisoformat(
                    scan_result.resolution_time.replace("Z", "+00:00")
                )
                hours_left = (res_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_left <= config.KALSHI_TIME_URGENCY_HOURS:
                    confidence += config.KALSHI_TIME_BOOST
            except Exception:  # noqa: BLE001
                pass

        return min(confidence, config.CONFIDENCE_CAP), direction
    except Exception as exc:  # noqa: BLE001
        log.error("score_kalshi failed: %s", exc)
        return 0.0, None


def adjust_for_news(confidence, direction, sentiment):
    """Return (adjusted_confidence, boost); boost of None means the signal is blocked."""
    boost = 0.0
    long_like = direction in ("long", "yes")
    short_like = direction in ("short", "no")

    if long_like and sentiment == "bullish":
        boost = config.NEWS_BOOST_ALIGNED
    elif long_like and sentiment == "bearish":
        boost = -config.NEWS_PENALTY_OPPOSED
    elif short_like and sentiment == "bearish":
        boost = config.NEWS_BOOST_ALIGNED
    elif short_like and sentiment == "bullish":
        boost = -config.NEWS_PENALTY_OPPOSED

    adjusted = confidence + boost
    if adjusted < config.NEWS_BLOCK_FLOOR and boost < 0:
        return adjusted, None  # strongly opposing news → block
    adjusted = max(0.0, min(adjusted, config.CONFIDENCE_CAP))
    return adjusted, boost


async def generate_signals(scan_results):
    """Score each scan result and return the surviving Signal objects."""
    signals = []
    macro_imminent, macro_name, _ = scanner.check_macro_events()

    for sr in scan_results:
        try:
            # Macro pause blocks everything except Kalshi (Kalshi IS the opportunity).
            if macro_imminent and sr.market != "kalshi":
                signals.append(
                    Signal(
                        market=sr.market,
                        symbol=sr.symbol,
                        direction=None,
                        confidence=0.0,
                        technical_score=0.0,
                        news_sentiment=sr.news_sentiment,
                        news_headline=sr.news_headline,
                        news_boost=0.0,
                        macro_blocked=True,
                        block_reason="macro_pause",
                        scan_result=sr,
                    )
                )
                continue

            if sr.market == "crypto":
                direction = detect_direction(sr.ohlcv)
                if direction is None:
                    continue
                tech_score = score_technical(sr.ohlcv, direction)
                confidence = tech_score / 100.0
            elif sr.market == "kalshi":
                confidence, direction = score_kalshi(sr)
                tech_score = confidence * 100.0
                if direction is None:
                    continue
            else:
                continue

            adjusted, boost = adjust_for_news(confidence, direction, sr.news_sentiment)
            if boost is None:
                signals.append(
                    Signal(
                        market=sr.market,
                        symbol=sr.symbol,
                        direction=direction,
                        confidence=adjusted,
                        technical_score=tech_score,
                        news_sentiment=sr.news_sentiment,
                        news_headline=sr.news_headline,
                        news_boost=0.0,
                        macro_blocked=True,
                        block_reason="news_opposing",
                        scan_result=sr,
                    )
                )
                continue

            if adjusted < config.MIN_CONFIDENCE:
                continue

            signals.append(
                Signal(
                    market=sr.market,
                    symbol=sr.symbol,
                    direction=direction,
                    confidence=adjusted,
                    technical_score=tech_score,
                    news_sentiment=sr.news_sentiment,
                    news_headline=sr.news_headline,
                    news_boost=boost,
                    macro_blocked=False,
                    block_reason="",
                    scan_result=sr,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.error("generate_signals failed for %s: %s", sr.symbol, exc)
            continue

    return signals
