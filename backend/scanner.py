"""RAID scanner — async market data from Kraken, Kalshi, NewsAPI, plus a macro calendar."""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

import config

log = logging.getLogger("raid.scanner")

KRAKEN_BASE = "https://api.kraken.com/0/public"
KALSHI_BASE = "https://trading-api.kalshi.com/trade-api/v2"
NEWS_BASE = "https://newsapi.org/v2/everything"
KRAKEN_FUTURES_BASE = "https://futures.kraken.com/derivatives/api/v3"

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
    ohlcv_1h: list = field(default_factory=list)  # 1-hour candles for HTF trend
    ohlcv_15m: list = field(default_factory=list)  # 15-minute candles for MTF trend
    ohlcv_30m: list = field(default_factory=list)  # 30-minute candles for MTF trend
    funding_rate: float = 0.0  # Kraken Futures perpetual rate (pos=crowded long, neg=crowded short)
    order_book: dict = field(default_factory=dict)  # Top 3 bid/ask walls by USD volume
    open_interest: float = 0.0  # Kraken Futures perpetual OI (contract value)
    fear_greed: int = 50  # Crypto Fear & Greed Index (0=extreme fear, 100=extreme greed)
    scan_time: str = None
    error: str = None


def _now_iso():
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ── Timeframe-aware OHLCV cache (5-min cycles: stay under Kraken rate limits) ──
# 5m refreshes every cycle, 15m every 3rd, 30m every 6th, 1h every 12th; a cold entry
# always fetches. Cuts avg OHLCV calls/cycle from ~96 to ~38. The cache is transparent to
# the runner — every ScanResult still carries all four timeframes each cycle.
_TF_REFRESH_CYCLES = {"5m": 1, "15m": 3, "30m": 6, "1h": 12}
_ohlcv_cache: dict = {}          # {symbol: {tf_key: (rows, fetched_at)}}
_cycle_counter = 0
_CALL_PACING_SECONDS = 0.15      # await between live Kraken calls to stay under the rate limit


def _refresh_due(tf_key: str, cycle: int, cold: bool) -> bool:
    """Whether a timeframe should re-fetch this cycle (pure, testable)."""
    if cold:
        return True
    return cycle % _TF_REFRESH_CYCLES.get(tf_key, 1) == 0


async def _fetch_ohlcv_cached(client, altname: str, interval: int, tf_key: str, candle_limit: int):
    """Return [ts,o,h,l,c,vol] rows for a timeframe, honoring the refresh schedule + cache.
    Never raises — returns cached rows (or []) on failure. Paces live calls."""
    cold = tf_key not in _ohlcv_cache.get(altname, {})
    if not _refresh_due(tf_key, _cycle_counter, cold):
        return _ohlcv_cache[altname][tf_key][0]
    try:
        res = await client.get(f"{KRAKEN_BASE}/OHLC", params={"pair": altname, "interval": interval})
        result = res.json().get("result", {})
        rows: list = []
        for k, v in result.items():
            if k == "last":
                continue
            rows = [[c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[6])] for c in v[-candle_limit:]]
            break
        _ohlcv_cache.setdefault(altname, {})[tf_key] = (rows, datetime.now(timezone.utc))
        await asyncio.sleep(_CALL_PACING_SECONDS)
        return rows
    except Exception as exc:  # noqa: BLE001
        cached = _ohlcv_cache.get(altname, {}).get(tf_key)
        if cached:
            log.warning("OHLC %s %s fetch failed (%s) — using cached", altname, tf_key, exc)
            return cached[0]
        log.error("OHLC %s %s fetch failed, no cache: %s", altname, tf_key, exc)
        return []


def _cache_fresh(fetched_at, now, ttl_minutes: float) -> bool:
    """True iff `fetched_at` is within ttl_minutes of `now` (pure, testable)."""
    if fetched_at is None:
        return False
    return (now - fetched_at).total_seconds() < ttl_minutes * 60


# Fear & Greed cache (30-min refresh — alternative.me updates ~hourly).
_fg_cache = {"value": None, "fetched_at": None}
_FG_CACHE_MINUTES = 30


# Kraken Futures symbol (PF_XBTUSD) -> Kraken Spot altname (XBTUSD) mapping
_FUTURES_TO_SPOT = {
    "XBT": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD",
    "DOGE": "XDGUSD", "ADA": "ADAUSD", "AVAX": "AVAXUSD", "LINK": "LINKUSD",
    "MATIC": "MATICUSD", "DOT": "DOTUSD", "LTC": "LTCUSD", "UNI": "UNIUSD",
    "AAVE": "AAVEUSD", "COMP": "COMPUSD", "ATOM": "ATOMUSD", "FIL": "FILUSD",
    "XLM": "XLMUSD", "XMR": "XMRUSD", "ZEC": "ZECUSD", "NEAR": "NEARUSD",
    "TRX": "TRXUSD", "SUI": "SUIUSD",
}


async def fetch_funding_rates() -> tuple:
    """Fetch perpetual funding rates AND open interest from Kraken Futures public API.
    Returns (rates_dict, oi_dict) e.g. ({"XBTUSD": 0.000023}, {"XBTUSD": 5432.1}).
    Positive rate = longs paying shorts = crowded long = short has contrarian edge.
    Returns ({}, {}) on any error — never raises."""
    rates = {}
    oi = {}
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
            res = await client.get(f"{KRAKEN_FUTURES_BASE}/tickers")
            tickers = res.json().get("tickers", [])
            for t in tickers:
                symbol = t.get("symbol", "")
                # Only perpetuals (PF_) carry funding rates; skip fixed futures (FI_)
                if not symbol.startswith("PF_"):
                    continue
                base = symbol[3:].replace("USD", "").replace("EUR", "")
                spot = _FUTURES_TO_SPOT.get(base)
                if not spot:
                    continue
                rate = t.get("fundingRate")
                if rate is not None:
                    try:
                        rates[spot] = float(rate)
                    except (TypeError, ValueError):
                        pass
                open_int = t.get("openInterest")
                if open_int is not None:
                    try:
                        oi[spot] = float(open_int)
                    except (TypeError, ValueError):
                        pass
        log.info("FUNDING: fetched %d rates, %d OI values", len(rates), len(oi))
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_funding_rates failed: %s", exc)
    return rates, oi


async def _fetch_order_book(client, pair: str) -> dict:
    """Fetch top 3 bid/ask walls from Kraken order book (reuses existing httpx client).
    Returns {"bid_walls": [{"price": X, "usd": Y}], "ask_walls": [...]} sorted by USD volume.
    Returns {} on error — never raises."""
    try:
        res = await client.get(
            f"{KRAKEN_BASE}/Depth", params={"pair": pair, "count": 25}
        )
        data = res.json().get("result", {})
        for _, book in data.items():
            bid_walls, ask_walls = [], []
            for e in book.get("bids", []):
                try:
                    p, v = float(e[0]), float(e[1])
                    bid_walls.append({"price": round(p, 6), "usd": round(p * v, 2)})
                except (IndexError, TypeError, ValueError):
                    continue
            for e in book.get("asks", []):
                try:
                    p, v = float(e[0]), float(e[1])
                    ask_walls.append({"price": round(p, 6), "usd": round(p * v, 2)})
                except (IndexError, TypeError, ValueError):
                    continue
            bid_walls.sort(key=lambda w: w["usd"], reverse=True)
            ask_walls.sort(key=lambda w: w["usd"], reverse=True)
            return {"bid_walls": bid_walls[:3], "ask_walls": ask_walls[:3]}
    except Exception as exc:  # noqa: BLE001
        log.error("_fetch_order_book failed for %s: %s", pair, exc)
    return {}


# Last-known global Fear & Greed index — cached so the worker (fill side) can read
# the current value without an extra API call. Updated each brain cycle by fetch_fear_greed().
LAST_FEAR_GREED = 50


async def fetch_fear_greed() -> int:
    """Fetch Crypto Fear & Greed Index from Alternative.me (free, no key needed).
    Returns value 0-100 (0=extreme fear, 100=extreme greed), or 50 (neutral) on error.
    Cached 30 min (the index updates ~hourly) so 5-min cycles don't get rate-limited."""
    global LAST_FEAR_GREED
    now = datetime.now(timezone.utc)
    if _fg_cache["value"] is not None and _cache_fresh(_fg_cache["fetched_at"], now, _FG_CACHE_MINUTES):
        age_m = int((now - _fg_cache["fetched_at"]).total_seconds() / 60)
        log.info("F&G: cached (%d, fetched %dm ago)", _fg_cache["value"], age_m)
        LAST_FEAR_GREED = _fg_cache["value"]
        return _fg_cache["value"]
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
            res = await client.get("https://api.alternative.me/fng/")
            data = res.json().get("data", [])
            if data:
                val = int(data[0].get("value", 50))
                LAST_FEAR_GREED = val
                _fg_cache["value"] = val
                _fg_cache["fetched_at"] = now
                log.info("FEAR_GREED: index=%d", val)
                return val
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_fear_greed failed: %s", exc)
    return 50


async def scan_kraken():
    """Scan liquid Kraken USD pairs and return a ScanResult per pair (never raises)."""
    global _cycle_counter
    _cycle_counter += 1
    if _cycle_counter == 1:
        log.info("SCANNER: timeframe cache enabled — 5m every cycle, 15m/3, 30m/6, 1h/12")
    # Pre-fetch funding rates + open interest once (separate Kraken Futures API — non-blocking on failure).
    funding_rates, oi_data = {}, {}
    try:
        funding_rates, oi_data = await fetch_funding_rates()
    except Exception as exc:  # noqa: BLE001
        log.warning("scan_kraken: funding rates unavailable: %s", exc)
    # Fetch global Fear & Greed once (same value for all symbols).
    fear_greed_value = 50
    try:
        fear_greed_value = await fetch_fear_greed()
    except Exception as exc:  # noqa: BLE001
        log.warning("scan_kraken: fear & greed unavailable: %s", exc)
    results = []
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
            pairs_res = await client.get(f"{KRAKEN_BASE}/AssetPairs")
            pairs_data = pairs_res.json().get("result", {})

            # All USD-quote pairs: altname -> canonical Ticker key.
            # Exclude non-crypto symbols (forex/gold) listed in config.EXCLUDED_SYMBOLS.
            excluded = set(getattr(config, "EXCLUDED_SYMBOLS", []))
            candidates = {}
            for pair_key, info in pairs_data.items():
                altname = info.get("altname")
                if info.get("quote") in config.KRAKEN_QUOTES and altname:
                    if altname in excluded:
                        continue
                    candidates[altname] = pair_key
            if not candidates:
                return results
            canon_to_alt = {canon: alt for alt, canon in candidates.items()}

            # Fetch Ticker for every candidate (chunked) and compute 24h USD volume.
            # Kraken Ticker: v[1] = 24h base volume, p[1] = 24h VWAP -> base*vwap = USD.
            prices = {}    # altname -> last price
            volumes = {}   # altname -> 24h USD volume
            altnames = list(candidates)
            for i in range(0, len(altnames), config.KRAKEN_TICKER_CHUNK):
                chunk = altnames[i : i + config.KRAKEN_TICKER_CHUNK]
                try:
                    tick_res = await client.get(
                        f"{KRAKEN_BASE}/Ticker", params={"pair": ",".join(chunk)}
                    )
                    tick_data = tick_res.json().get("result", {})
                    for canon, t in tick_data.items():
                        alt = canon_to_alt.get(canon, canon)
                        try:
                            prices[alt] = float(t["c"][0])
                            volumes[alt] = float(t["v"][1]) * float(t["p"][1])
                        except (KeyError, IndexError, TypeError, ValueError):
                            continue
                except Exception as exc:  # noqa: BLE001
                    log.error("Kraken Ticker chunk failed: %s", exc)
                    continue

            # Keep only pairs above the USD-volume floor, highest first, then cap.
            # Always include priority pairs (volatile/meme coins — scanned regardless of volume).
            priority = set(getattr(config, "PRIORITY_PAIRS", []))
            priority_in = [a for a in candidates if a in priority]
            # Fill remaining slots with top volume-sorted non-priority pairs.
            volume_pairs = sorted(
                (a for a in candidates if volumes.get(a, 0.0) >= config.MIN_24H_USD_VOLUME and a not in priority),
                key=lambda a: volumes.get(a, 0.0),
                reverse=True,
            )[: config.KRAKEN_MAX_PAIRS]
            liquid = list(dict.fromkeys(priority_in + volume_pairs))  # dedupe, priority first
            log.info("SCAN: %d pairs (%d priority, %d volume)", len(liquid), len(priority_in), len(volume_pairs))
            if not liquid:
                log.warning(
                    "No Kraken pairs above $%.0f 24h volume", config.MIN_24H_USD_VOLUME
                )
                return results

            for altname in liquid:
                try:
                    # Timeframe-aware cached OHLCV (5m every cycle; 15m/3, 30m/6, 1h/12).
                    ohlcv = await _fetch_ohlcv_cached(client, altname, config.KRAKEN_OHLC_INTERVAL, "5m", config.OHLCV_CANDLES)
                    current = prices.get(altname)
                    if current is None and ohlcv:
                        current = ohlcv[-1][4]
                    ohlcv_1h = await _fetch_ohlcv_cached(client, altname, 60, "1h", 60)
                    ohlcv_15m = await _fetch_ohlcv_cached(client, altname, 15, "15m", 60)
                    ohlcv_30m = await _fetch_ohlcv_cached(client, altname, 30, "30m", 60)
                    # Order book depth — fetched EVERY cycle (C10 sweep detection needs
                    # current depth), not cached. Paced like the OHLCV calls.
                    order_book_data = {}
                    try:
                        order_book_data = await _fetch_order_book(client, altname)
                        await asyncio.sleep(_CALL_PACING_SECONDS)
                    except Exception as exc:  # noqa: BLE001
                        log.error("Order book fetch failed for %s: %s", altname, exc)
                    results.append(
                        ScanResult(
                            market="crypto",
                            symbol=altname,
                            ohlcv=ohlcv,
                            ohlcv_1h=ohlcv_1h,
                            ohlcv_15m=ohlcv_15m,
                            ohlcv_30m=ohlcv_30m,
                            current_price=current or 0.0,
                            volume_24h=volumes.get(altname),
                            funding_rate=funding_rates.get(altname, 0.0),
                            order_book=order_book_data,
                            open_interest=oi_data.get(altname, 0.0),
                            fear_greed=fear_greed_value,
                            scan_time=_now_iso(),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    log.error("Kraken scan failed for %s: %s", altname, exc)
                    continue
    except Exception as exc:  # noqa: BLE001
        log.error("scan_kraken failed: %s", exc)
    return results


async def scan_kalshi():
    """DISABLED — Kalshi API returns 401 Unauthorized; crypto only until auth is fixed."""
    # Disabled 2026-06-22: every Kalshi call 401s. Returning [] so the worker runs
    # crypto-only. Re-enable by restoring the body below once Kalshi auth works.
    return []
    # results = []
    # try:
    #     headers = {}
    #     if config.KALSHI_API_KEY:
    #         headers["Authorization"] = f"Bearer {config.KALSHI_API_KEY}"
    #     async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, headers=headers) as client:
    #         res = await client.get(f"{KALSHI_BASE}/markets", params={"status": "open", "limit": 200})
    #         markets = res.json().get("markets", [])
    #         now = datetime.now(timezone.utc)
    #         horizon = now + timedelta(hours=config.KALSHI_CLOSE_WITHIN_HOURS)
    #         for m in markets:
    #             try:
    #                 close_raw = m.get("close_time")
    #                 if not close_raw:
    #                     continue
    #                 close_dt = datetime.fromisoformat(close_raw.replace("Z", "+00:00"))
    #                 if not (now <= close_dt <= horizon):
    #                     continue
    #                 yes_price = (m.get("yes_ask") or 0) / 100.0
    #                 no_price = (m.get("no_ask") or 0) / 100.0
    #                 results.append(
    #                     ScanResult(
    #                         market="kalshi",
    #                         symbol=m.get("ticker", m.get("id", "")),
    #                         yes_price=yes_price,
    #                         no_price=no_price,
    #                         current_price=yes_price,
    #                         volume_24h=float(m.get("volume_24h", m.get("volume", 0)) or 0),
    #                         resolution_time=close_raw,
    #                         market_id=m.get("ticker", m.get("id")),
    #                         scan_time=_now_iso(),
    #                     )
    #                 )
    #             except Exception as exc:  # noqa: BLE001
    #                 log.error("Kalshi market parse failed: %s", exc)
    #                 continue
    # except Exception as exc:  # noqa: BLE001
    #     log.error("scan_kalshi failed: %s", exc)
    # return results


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


async def scan_news(symbols: list):
    """Fetch recent crypto headlines and match to symbols.
    Uses CryptoCompare News API (free, no key needed, works from deployed servers).
    Returns {symbol: {headline, sentiment, published_at}}."""
    out = {}
    if not symbols:
        return out
    # Default all symbols to neutral/no-news
    for sym in symbols:
        out[sym] = {"headline": None, "sentiment": "neutral", "published_at": None}
    if not getattr(config, "NEWS_ENABLED", True):
        return out   # CryptoCompare disabled (rate-limited); deterministic engine uses no news
    try:
        cc_key = os.environ.get("CRYPTOCOMPARE_API_KEY", "")
        headers = {"Authorization": f"Apikey {cc_key}"} if cc_key else {}
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, headers=headers) as client:
            res = await client.get(
                "https://min-api.cryptocompare.com/data/v2/news/",
                params={"lang": "EN", "sortOrder": "latest"},
            )
            response_json = res.json()
            log.info("NEWS DEBUG: status=%d resp_type=%s msg=%s",
                     res.status_code,
                     response_json.get("Type"),
                     str(response_json.get("Message", ""))[:100])
            raw_data = response_json.get("Data") or []
            articles = raw_data[:50] if isinstance(raw_data, list) else []
            log.info("NEWS: got %d articles from CryptoCompare (key=%s)",
                     len(articles), "YES" if cc_key else "NO")
            if not articles:
                return out
            # Build symbol lookup: strip "USD" suffix for matching
            # e.g. "BTCUSD" -> "BTC", "ETHUSD" -> "ETH"
            sym_map = {}
            # Kraken ticker -> CryptoCompare ticker aliases
            kraken_to_cc = {"XBT": "BTC", "XDG": "DOGE"}
            for sym in symbols:
                base = sym.replace("USD", "").upper()
                sym_map[base] = sym
                # Also map the CryptoCompare alias back to this symbol
                if base in kraken_to_cc:
                    sym_map[kraken_to_cc[base]] = sym
                # Reverse: if someone passes BTCUSD, also map XBT
            cc_to_kraken = {v: k for k, v in kraken_to_cc.items()}
            for sym in symbols:
                base = sym.replace("USD", "").upper()
                if base in cc_to_kraken:
                    sym_map[cc_to_kraken[base]] = sym
            # Also map full names for common cryptos
            name_map = {
                "BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL",
                "RIPPLE": "XRP", "CARDANO": "ADA", "POLKADOT": "DOT",
                "AVALANCHE": "AVAX", "CHAINLINK": "LINK", "LITECOIN": "LTC",
                "MONERO": "XMR", "STELLAR": "XLM", "DOGECOIN": "XDG",
                "NEAR PROTOCOL": "NEAR", "NEAR": "NEAR", "AAVE": "AAVE",
                "COMPOUND": "COMP", "ZCASH": "ZEC", "TRON": "TRX",
                "SUI": "SUI",
            }
            for article in articles:
                title = (article.get("title") or "").upper()
                categories = (article.get("categories") or "").upper()
                body_preview = (article.get("body") or "")[:200].upper()
                published = article.get("published_on")
                pub_str = None
                if published:
                    from datetime import timezone as tz
                    pub_str = datetime.fromtimestamp(published, tz=tz.utc).isoformat()
                # Check which symbols this article matches
                matched_bases = set()
                # Match by CryptoCompare categories (pipe-separated: "BTC|ETH|MARKET")
                for cat in categories.split("|"):
                    cat = cat.strip()
                    if cat in sym_map:
                        matched_bases.add(cat)
                # Match by full name in title
                for full_name, base in name_map.items():
                    if full_name in title and base in sym_map:
                        matched_bases.add(base)
                # Match by ticker in title (e.g. "BTC" in headline)
                for base in sym_map:
                    if base in title:
                        matched_bases.add(base)
                # Assign to first unassigned symbol match
                for base in matched_bases:
                    full_sym = sym_map[base]
                    if out[full_sym]["headline"] is None:
                        combined = f"{article.get('title', '')} {body_preview}"
                        out[full_sym] = {
                            "headline": article.get("title"),
                            "sentiment": _score_sentiment(combined),
                            "published_at": pub_str,
                        }
        matched = [s for s in out if out[s].get("headline")]
        log.info("NEWS: matched %d/%d symbols: %s", len(matched), len(symbols),
                 matched[:10] if matched else "none")
    except Exception as exc:  # noqa: BLE001
        log.error("scan_news (CryptoCompare) failed: %s", exc)
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


_KRAKEN_PAIR_MAP = {}  # altname -> canonical Ticker key (cached; AssetPairs is static)


async def _kraken_pair_map(client):
    """Return (and cache) a Kraken altname -> canonical-pair-key map."""
    global _KRAKEN_PAIR_MAP
    if _KRAKEN_PAIR_MAP:
        return _KRAKEN_PAIR_MAP
    try:
        res = await client.get(f"{KRAKEN_BASE}/AssetPairs")
        data = res.json().get("result", {})
        _KRAKEN_PAIR_MAP = {
            info["altname"]: key for key, info in data.items() if info.get("altname")
        }
    except Exception as exc:  # noqa: BLE001
        log.error("_kraken_pair_map failed: %s", exc)
    return _KRAKEN_PAIR_MAP


async def fetch_kraken_prices(symbols):
    """Return {symbol: last_price} for many Kraken pairs in a single Ticker call."""
    out = {}
    syms = [s for s in dict.fromkeys(symbols) if s]  # dedupe, preserve order
    if not syms:
        return out
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
            res = await client.get(f"{KRAKEN_BASE}/Ticker", params={"pair": ",".join(syms)})
            result = res.json().get("result", {})
            pair_map = await _kraken_pair_map(client)
            for sym in syms:
                # Kraken keys results by canonical name; map our altname to it,
                # falling back to a direct/contains match.
                canonical = pair_map.get(sym, sym)
                t = result.get(canonical) or result.get(sym)
                if t is None:
                    t = next((v for k, v in result.items() if sym in k), None)
                if t is None:
                    continue
                try:
                    out[sym] = float(t["c"][0])
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_kraken_prices failed: %s", exc)
    return out


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
