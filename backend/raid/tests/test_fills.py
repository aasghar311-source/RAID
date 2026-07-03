"""Tests for the realistic fill simulator."""

from raid.core.marketdata import OrderBookSnapshot
from raid.execution.fills import (
    simulate_taker, simulate_maker_limit, simulate_stop_exit, TAKER_FEE_PCT,
)


def _book(bids, asks):
    return OrderBookSnapshot(ts=0, bids=tuple(bids), asks=tuple(asks))


def test_taker_buy_walks_levels_vwap_and_slippage():
    book = _book(bids=[(99.0, 5)], asks=[(100.0, 1.0), (101.0, 2.0)])
    r = simulate_taker(book, qty=2.0, side="buy")
    assert not r.rejected and not r.is_partial
    assert r.filled_qty == 2.0
    assert abs(r.avg_price - 100.5) < 1e-9        # (1*100 + 1*101)/2
    assert abs(r.slippage_cost - 1.0) < 1e-9      # (100.5-100)*2
    assert abs(r.fee_paid - 100.5 * 2 * TAKER_FEE_PCT) < 1e-9


def test_taker_partial_when_depth_insufficient():
    book = _book(bids=[], asks=[(100.0, 1.0), (100.5, 0.5)])
    r = simulate_taker(book, qty=5.0, side="buy")
    assert r.is_partial and r.filled_qty == 1.5 and not r.rejected


def test_taker_empty_book_rejects():
    r = simulate_taker(_book([], []), qty=1.0, side="buy")
    assert r.rejected and r.reason == "empty_book"


def test_taker_sell_walks_bids():
    book = _book(bids=[(100.0, 1.0), (99.0, 3.0)], asks=[(101.0, 1)])
    r = simulate_taker(book, qty=2.0, side="sell")
    assert abs(r.avg_price - 99.5) < 1e-9         # (1*100 + 1*99)/2
    assert abs(r.slippage_cost - 1.0) < 1e-9      # (100-99.5)*2


def test_maker_limit_only_fills_when_touched():
    assert simulate_maker_limit(1.0, 100.0, "buy", touched=False).rejected
    r = simulate_maker_limit(2.0, 100.0, "buy", touched=True)
    assert not r.rejected and r.avg_price == 100.0 and r.slippage_cost == 0.0
    # partial via limited resting liquidity
    rp = simulate_maker_limit(5.0, 100.0, "buy", touched=True, available_qty=2.0)
    assert rp.is_partial and rp.filled_qty == 2.0


def test_stop_exit_gaps_through():
    # Long stop at 99; market gapped to 97 -> fill 97, slippage 2*qty, gapped.
    r = simulate_stop_exit(stop_price=99.0, market_price=97.0, qty=3.0, side="sell")
    assert abs(r.avg_price - 97.0) < 1e-9
    assert abs(r.slippage_cost - 6.0) < 1e-9
    assert r.reason == "stop_gapped"


def test_stop_exit_at_level_no_gap():
    # Market above the stop -> fills at the stop, no gap slippage.
    r = simulate_stop_exit(stop_price=99.0, market_price=99.5, qty=1.0, side="sell")
    assert abs(r.avg_price - 99.0) < 1e-9 and r.slippage_cost == 0.0
    assert r.reason == "stop_at_level"


def test_short_stop_exit_gaps_up():
    # Short stop at 101; market gapped to 103 -> fill 103 (worse for a short cover).
    r = simulate_stop_exit(stop_price=101.0, market_price=103.0, qty=2.0, side="buy")
    assert abs(r.avg_price - 103.0) < 1e-9 and abs(r.slippage_cost - 4.0) < 1e-9
