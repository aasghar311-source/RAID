"""Exit-price-source tests: live quote (bid long / ask short) with a fail-closed fallback.

Last-trade (c[0]) freezes for minutes between prints on illiquid pairs; exits now read the
continuously-requoting book. A long exits at the BID, a short at the ASK; an invalid/crossed/
one-sided/too-wide book falls back to last-trade with a labelled reason. Entries are unaffected
(they use scanner.scan_kraken, not this path).

Discovered and run by raid.tests.run_all (plain asserts, no pytest).
"""

import asyncio

import config
import executor

MSPREAD = config.MAX_EXIT_SPREAD_PCT   # 0.02


def _q(last, bid, ask):
    return {"last": last, "bid": bid, "ask": ask}


def test_long_exits_at_bid():
    p, side = executor.exit_price_from_quote("long", _q(100.0, 99.9, 100.1), MSPREAD)
    assert side == "bid" and p == 99.9, (p, side)


def test_short_exits_at_ask():
    p, side = executor.exit_price_from_quote("short", _q(100.0, 99.9, 100.1), MSPREAD)
    assert side == "ask" and p == 100.1, (p, side)


def test_crossed_book_falls_back_to_last():
    p, side = executor.exit_price_from_quote("long", _q(100.0, 100.2, 100.1), MSPREAD)  # bid>ask
    assert p == 100.0 and side.startswith("last"), (p, side)


def test_zero_or_one_sided_book_falls_back():
    p, side = executor.exit_price_from_quote("long", _q(100.0, 0.0, 100.1), MSPREAD)
    assert p == 100.0 and "invalid_book" in side
    p2, s2 = executor.exit_price_from_quote("short", _q(50.0, 49.9, 0.0), MSPREAD)
    assert p2 == 50.0 and "invalid_book" in s2


def test_wide_spread_falls_back_to_last():
    # 5% spread (97.5/102.5) exceeds the 2% cap -> fall back to last-trade.
    p, side = executor.exit_price_from_quote("long", _q(100.0, 97.5, 102.5), MSPREAD)
    assert p == 100.0 and "wide_spread" in side, (p, side)


def test_quote_moves_while_last_trade_frozen():
    # last-trade frozen at 0.417800; the bid steps up -> a long exit tracks the moving BID,
    # never the frozen last-trade (the whole point of the fix).
    frozen = 0.417800
    got = [executor.exit_price_from_quote("long", _q(frozen, b, b + 0.0005), MSPREAD)[0]
           for b in (0.418000, 0.418500, 0.419000)]
    assert got == [0.418000, 0.418500, 0.419000], got
    assert all(p != frozen for p in got)


def test_exit_price_helper_uses_quote_for_crypto_long():
    trade = {"market": "crypto", "symbol": "SOLUSD", "direction": "long"}
    quotes = {"SOLUSD": _q(100.0, 99.9, 100.1)}
    price, q, side = asyncio.run(executor._exit_price(trade, quotes))
    assert side == "bid" and price == 99.9 and q is quotes["SOLUSD"]
