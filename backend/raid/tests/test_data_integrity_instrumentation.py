"""B2 + B3 measure-first instrumentation — completed-candle would-drop + real spread/depth.

Both are LOG-ONLY: they compute what enforcement WOULD see without changing any decision. The pure
helpers are tested directly. No DB, no network. Auto-discovered by raid.tests.run_all.
"""

from raid.runner import completed_candle_would_drop, _real_spread_depth


def test_completed_candle_would_drop_detects_forming_bar():
    now = 1_000_000_000                       # arbitrary epoch (seconds)
    window_start = (now // 300) * 300
    # last bar opened in the current (unfinished) window -> still forming -> would drop
    wd, lts, age = completed_candle_would_drop([[window_start, 1, 1, 1, 1, 1]], now)
    assert wd is True and lts == window_start and age == now - window_start
    # last bar opened a full interval ago -> completed -> would NOT drop
    wd2, _, _ = completed_candle_would_drop([[window_start - 300, 1, 1, 1, 1, 1]], now)
    assert wd2 is False
    # empty / None -> safe False
    assert completed_candle_would_drop([], now)[0] is False
    assert completed_candle_would_drop(None, now)[0] is False


def test_real_spread_depth_reads_bid_walls_ask_walls():
    ob = {"bid_walls": [{"price": 99.0, "usd": 5000.0}, {"price": 98.0, "usd": 3000.0}],
          "ask_walls": [{"price": 101.0, "usd": 4000.0}, {"price": 102.0, "usd": 2000.0}]}
    spread, depth, ok = _real_spread_depth(ob)
    assert ok is True
    assert abs(spread - (101.0 - 99.0) / 100.0) < 1e-9        # (ask-bid)/mid, mid=100
    assert depth == 5000.0 + 3000.0 + 4000.0 + 2000.0          # summed executable depth (USD)


def test_real_spread_depth_rejects_legacy_and_empty_shapes():
    # The OLD buggy shape ('bids'/'asks') is NOT read -> (None, None, False), proving the fix
    # depends on the correct bid_walls/ask_walls keys (the runner still feeds decisions the fallback).
    legacy = {"bids": [[99.0, 1.0]], "asks": [[101.0, 1.0]]}
    assert _real_spread_depth(legacy) == (None, None, False)
    assert _real_spread_depth({}) == (None, None, False)
    assert _real_spread_depth(None) == (None, None, False)
    # crossed / one-sided book -> not usable
    crossed = {"bid_walls": [{"price": 102.0, "usd": 1.0}], "ask_walls": [{"price": 101.0, "usd": 1.0}]}
    assert _real_spread_depth(crossed) == (None, None, False)
