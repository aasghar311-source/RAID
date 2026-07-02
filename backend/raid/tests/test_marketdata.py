"""Tests for normalized market data + data-quality validation + Kraken normalizers."""

from raid.core.marketdata import (
    NormalizedBar, OrderBookSnapshot, Severity,
    validate_bars, validate_order_book,
)
from raid.core.provider import normalize_kraken_ohlc, normalize_kraken_orderbook


def _bars(seq):
    # seq: list of (ts, o, h, l, c)
    return [NormalizedBar(ts=t, open=o, high=h, low=l, close=c, volume=1.0) for t, o, h, l, c in seq]


def test_valid_bars_no_critical():
    bars = _bars([(0, 10, 11, 9, 10.5), (60, 10.5, 12, 10, 11), (120, 11, 11.5, 10.5, 11.2)])
    ev = validate_bars("SOLUSD", "5m", bars)
    assert not any(e.severity == Severity.CRITICAL for e in ev)


def test_insufficient_bars_critical():
    ev = validate_bars("SOLUSD", "5m", _bars([(0, 10, 11, 9, 10)]))
    assert any(e.kind == "insufficient_bars" for e in ev)


def test_nonpositive_and_ohlc_inconsistent():
    ev = validate_bars("SOLUSD", "5m", _bars([(0, 10, 11, 9, 10), (60, 0, 1, 0, 0)]))
    assert any(e.kind == "nonpositive_price" and e.severity == Severity.CRITICAL for e in ev)
    # high below open/close
    ev2 = validate_bars("SOLUSD", "5m", _bars([(0, 10, 11, 9, 10), (60, 10, 9, 8, 12)]))
    assert any(e.kind == "ohlc_inconsistent" for e in ev2)


def test_timestamp_not_increasing_critical():
    ev = validate_bars("SOLUSD", "5m", _bars([(60, 10, 11, 9, 10), (60, 10, 11, 9, 10)]))
    assert any(e.kind == "ts_not_increasing" for e in ev)


def test_order_book_validation():
    empty = validate_order_book("SOLUSD", OrderBookSnapshot(0, (), ()))
    assert any(e.kind == "empty_order_book" for e in empty)
    crossed = validate_order_book("SOLUSD", OrderBookSnapshot(0, ((101, 1),), ((100, 1),)))
    assert any(e.kind == "crossed_book" for e in crossed)
    ok = OrderBookSnapshot(0, ((100.0, 5),), ((100.2, 5),))
    assert ok.spread_pct is not None and 0 < ok.spread_pct < 0.01
    assert not any(e.severity == Severity.CRITICAL for e in validate_order_book("SOLUSD", ok))
    wide = OrderBookSnapshot(0, ((100.0, 5),), ((105.0, 5),))
    assert any(e.kind == "wide_spread" for e in validate_order_book("SOLUSD", wide))


def test_normalize_kraken_ohlc():
    raw = [[1700000000, "1.0", "2.0", "0.5", "1.5", "1.4", "10.0", 5]]
    bars = normalize_kraken_ohlc(raw)
    assert len(bars) == 1
    b = bars[0]
    assert b.ts == 1700000000 and b.high == 2.0 and b.volume == 10.0
    # malformed row skipped, not fabricated
    assert normalize_kraken_ohlc([["bad"]]) == []


def test_normalize_kraken_orderbook():
    raw = {"bids": [["100.0", "1.0", 1], ["99.9", "2.0", 1]], "asks": [["100.1", "3.0", 1]]}
    ob = normalize_kraken_orderbook(raw, ts=123)
    assert ob.best_bid == 100.0 and ob.best_ask == 100.1
    assert ob.depth_usd("bid", 5) == 100.0 * 1.0 + 99.9 * 2.0
