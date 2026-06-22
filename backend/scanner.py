"""RAID scanner — async market data from Kraken, Kalshi, NewsAPI, plus a macro calendar."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

import config

log = logging.getLogger("raid.scanner")

KRAKEN_BASE = "https://api.kraken.com/0/public"
KALSHI_BASE = "https://trading-api.kalshi.com/trade-api/v2"
NEWS_BASE = "https://newsapi.org/v2/everything"

# 2026 macro calendar (UTC). Times are release/decision times.
MACRO_EVENTS = [
    (datetime(2026, 7, 10, 12, 30, tzinfo=timezone.utc), "CPI"),
    (datetime(2026, 7, 11, 12, 30, tzinfo=timezone.utc), "Core CPI"),
    (datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc), "PPI"),
    (datetime(2026, 7, 16, 12, 30, tzinfo=timezone.utc), "Retail Sales"),
    (datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc), "FOMC Decision"),
    (datetime(2026, 7, 30, 12, 30, tzinfo=timezone.utc), "GDP"),
    (datetime(2026, 8, 8, 12, 30, tzinfo=timezone.utc), "Payrolls"),
    (datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc), "CPI"),
    (datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc), "FOMC Decision"),
    (datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc), "PCE"),
    (datetime(2026, 10, 2, 12, 30, tzinfo=timezone.utc), "Payrolls"),
    (datetime(2026, 10, 14, 12, 30, tzinfo=timezone.utc), "CPI"),
    (datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc), "FOMC Decision"),
    (datetime(2026, 11, 6, 12, 30, tzinfo=timezone.utc), "Payrolls"),
    (datetime(2026, 12, 10, 12, 30, tzinfo=timezone.utc), "CPI"),
    (datetime(2026, 12, 16, 18, 0, tzinfo=timezone.utc), "FOMC Decision"),
]


@dataclass
class ScanResult:
    """A single market observation with everything signals.py needs to score it."""

    market: str
    symbol: str
    ohlcv: list = field(default_factory=list)  # [ts, open, high, low, close, volume]
    current_price: float = 0.0
    yes_price: float = None
    no_price: float = None
    volume_24h: float = None
    resolution_time: str = None
    market_id: str = None
    news_headline: str = None
    news_sentiment: str = "neutral"
    news_published: str = None
    macro_event_imminent: bool = False
    macro_event_name: str = None
    macro_minutes_until: int = None
    scan_time: str = None
    error: str = None


def _now_iso():
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


async def scan_kraken():
    """Scan liquid Kraken USD pairs and return a ScanResult per pair (never raises)."""
    results = []
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
            pairs_res = await client.get(f"{KRAKEN_BASE}/AssetPairs")
            pairs_data = pairs_res.json().get("result", {})

            selected = []
            for pair_key, info in pairs_data.items():
                if info.get("quote") in config.KRAKEN_QUOTES and "altname" in info:
                    selected.append((pair_key, info["altname"]))
                if len(selected) >= config.KRAKEN_MAX_PAIRS:
                    break

            if not selected:
                return results

            # One Ticker call for all selected pairs.
            altnames = ",".join(alt for _, alt in selected)
            prices = {}
            try:
                tick_res = await client.get(f"{KRAKEN_BASE}/Ticker", params={"pair": altnames})
                tick_data = tick_res.json().get("result", {})
                for key, t in tick_data.items():
                    prices[key] = float(t["c"][0])
            except Exception as exc:  # noqa: BLE001
                log.error("Kraken Ticker failed: %s", exc)

            for pair_key, altname in selected:
                try:
                    ohlc_res = await client.get(
                        f"{KRAKEN_BASE}/OHLC",
                        params={"pair": altname, "interval": config.KRAKEN_OHLC_INTERVAL},
                    )
                    ohlc_data = ohlc_res.json().get("result", {})
                    candles = []
                    for k, v in ohlc_data.items():
                        if k == "last":
                            continue
                        candles = v
                        break
                    # Kraken OHLC row: [time, open, high, low, close, vwap, volume, count]
                    ohlcv = [
                        [c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[6])]
                        for c in candles[-config.OHLCV_CANDLES:]
                    ]
                    current = prices.get(pair_key)
                    if current is None and ohlcv:
                        current = ohlcv[-1][4]
                    results.append(
                        ScanResult(
                            market="crypto",
                            symbol=altname,
                            ohlcv=ohlcv,
                            current_price=current or 0.0,
                            scan_time=_now_iso(),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    log.error("Kraken OHLC failed for %s: %s", altname, exc)
                    continue
    except Exception as exc:  # noqa: BLE001
        log.error("scan_kraken failed: %s", exc)
    return results


async def scan_kalshi():
    """Scan open Kalshi markets resolving within 24h; return a ScanResult per market."""
    results = []
    try:
        headers = {}
        if config.KALSHI_API_KEY:
            headers["Authorization"] = f"Bearer {config.KALSHI_API_KEY}"
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, headers=headers) as client:
            res = await client.get(f"{KALSHI_BASE}/markets", params={"status": "open", "limit": 200})
            markets = res.json().get("markets", [])
            now = datetime.now(timezone.utc)
            horizon = now + timedelta(hours=config.KALSHI_CLOSE_WITHIN_HOURS)
            for m in markets:
                try:
                    close_raw = m.get("close_time")
                    if not close_raw:
                        continue
                    close_dt = datetime.fromisoformat(close_raw.replace("Z", "+00:00"))
                    if not (now <= close_dt <= horizon):
                        continue
                    yes_price = (m.get("yes_ask") or 0) / 100.0
                    no_price = (m.get("no_ask") or 0) / 100.0
                    results.append(
                        ScanResult(
                            market="kalshi",
                            symbol=m.get("ticker", m.get("id", "")),
                            yes_price=yes_price,
                            no_price=no_price,
                            current_price=yes_price,
                            volume_24h=float(m.get("volume_24h", m.get("volume", 0)) or 0),
                            resolution_time=close_raw,
                            market_id=m.get("ticker", m.get("id")),
                            scan_time=_now_iso(),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    log.error("Kalshi market parse failed: %s", exc)
                    continue
    except Exception as exc:  # noqa: BLE001
        log.error("scan_kalshi failed: %s", exc)
    return results


def _score_sentiment(text: str):
    """Return 'bullish'/'bearish'/'neutral' from bullish vs bearish word counts."""
    lowered = (text or "").lower()
    bull = sum(lowered.count(w) for w in config.BULLISH_WORDS)
    bear = sum(lowered.count(w) for w in config.BEARISH_WORDS)
    if bull > bear:
        return "bullish"
    if bear > bull:
        return "bearish"
    return "neutral"


async def scan_news(symbols):
    """Fetch recent headlines per symbol and return {symbol: {headline, sentiment, published_at}}."""
    out = {}
    if not symbols:
        return out
    from_dt = (
        datetime.now(timezone.utc) - timedelta(hours=config.NEWS_LOOKBACK_HOURS)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
            for symbol in symbols:
                try:
                    res = await client.get(
                        NEWS_BASE,
                        params={
                            "q": symbol,
                            "sortBy": "publishedAt",
                            "from": from_dt,
                            "pageSize": config.NEWS_TOP_N,
                            "language": "en",
                            "apiKey": config.NEWS_API_KEY,
                        },
                    )
                    articles = res.json().get("articles", [])[: config.NEWS_TOP_N]
                    if not articles:
                        out[symbol] = {"headline": None, "sentiment": "neutral", "published_at": None}
                        continue
                    combined = " ".join(
                        f"{a.get('title', '')} {a.get('description', '')}" for a in articles
                    )
                    out[symbol] = {
                        "headline": articles[0].get("title"),
                        "sentiment": _score_sentiment(combined),
                        "published_at": articles[0].get("publishedAt"),
                    }
                except Exception as exc:  # noqa: BLE001
                    log.error("scan_news failed for %s: %s", symbol, exc)
                    out[symbol] = {"headline": None, "sentiment": "neutral", "published_at": None}
    except Exception as exc:  # noqa: BLE001
        log.error("scan_news failed: %s", exc)
    return out


def check_macro_events():
    """Return (is_imminent, event_name, minutes_until) for the nearest blocking macro event."""
    try:
        now = datetime.now(timezone.utc)
        for event_dt, name in MACRO_EVENTS:
            delta_min = int((event_dt - now).total_seconds() // 60)
            # Pre-event window: within MACRO_PAUSE_MINUTES_BEFORE before the event.
            if 0 <= delta_min <= config.MACRO_PAUSE_MINUTES_BEFORE:
                return True, name, delta_min
            # Post-event window: within MACRO_RESUME_MINUTES_AFTER after the event.
            if -config.MACRO_RESUME_MINUTES_AFTER <= delta_min < 0:
                return True, name, delta_min
    except Exception as exc:  # noqa: BLE001
        log.error("check_macro_events failed: %s", exc)
    return False, None, None


async def fetch_kraken_price(symbol: str):
    """Return the last trade price for a Kraken pair, or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
            res = await client.get(f"{KRAKEN_BASE}/Ticker", params={"pair": symbol})
            data = res.json().get("result", {})
            for _, t in data.items():
                return float(t["c"][0])
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_kraken_price failed for %s: %s", symbol, exc)
    return None


async def fetch_kalshi_price(market_id: str):
    """Return the current yes price (0-1) for a Kalshi market, or None on failure."""
    try:
        headers = {}
        if config.KALSHI_API_KEY:
            headers["Authorization"] = f"Bearer {config.KALSHI_API_KEY}"
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, headers=headers) as client:
            res = await client.get(f"{KALSHI_BASE}/markets/{market_id}")
            m = res.json().get("market", {})
            yes_ask = m.get("yes_ask")
            if yes_ask is not None:
                return yes_ask / 100.0
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_kalshi_price failed for %s: %s", market_id, exc)
    return None
