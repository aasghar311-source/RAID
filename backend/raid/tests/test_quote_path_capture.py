"""B5 — quote-path flight recorder: correct record + NON-BLOCKING flush (exit loop unaffected).

The per-tick capture is a synchronous buffer append (no I/O); the batched DB flush is fire-and-
forget so it can never block the 1s exit loop. No real DB, no network. Auto-discovered by
raid.tests.run_all.
"""

import asyncio

import executor


def _clear():
    executor._quote_path_buffer.clear()


def test_buffer_quote_path_records_expected_fields():
    _clear()
    trade = {"id": "t-1", "symbol": "SOLUSD", "direction": "long",
             "peak_pnl_pct": 1.2, "mae_pct": -0.4}
    quote = {"bid": 99.0, "ask": 101.0, "last": 100.0}
    executor._buffer_quote_path(trade, 99.0, quote, "bid", 0.5)
    assert len(executor._quote_path_buffer) == 1
    rec = executor._quote_path_buffer[0]
    assert rec["trade_id"] == "t-1" and rec["pair"] == "SOLUSD"
    assert rec["bid"] == 99.0 and rec["ask"] == 101.0 and rec["mid"] == 100.0
    assert abs(rec["spread"] - (2.0 / 100.0)) < 1e-9
    assert rec["effective_exit_price"] == 99.0 and rec["direction"] == "long"
    assert rec["mfe"] == 1.2 and rec["mae"] == -0.4
    assert rec["source"] == "bid" and rec["quote_validity"] is True and rec["freshness_s"] == 0.5
    _clear()


def test_buffer_quote_path_never_raises_on_bad_input():
    _clear()
    executor._buffer_quote_path(None, None, None, None, 0.0)                 # must not raise
    executor._buffer_quote_path({"id": "x"}, 1.0, {"bid": 0, "ask": 0}, "last(invalid_book)", 0.0)
    rec = executor._quote_path_buffer[-1]
    assert rec["quote_validity"] is False and rec["mid"] is None and rec["spread"] is None
    _clear()


def test_flush_is_fire_and_forget_and_does_not_block():
    async def _run():
        done = []

        async def slow():
            await asyncio.sleep(0.05)
            done.append(True)

        t = executor._spawn_flush(slow())
        # _spawn_flush returned WITHOUT awaiting slow() -> it has not completed yet (non-blocking)
        assert done == []
        assert t in executor._quote_flush_tasks
        await asyncio.sleep(0.12)                       # let the background flush finish
        assert done == [True]
        assert t not in executor._quote_flush_tasks     # the done-callback dropped the reference
    asyncio.run(_run())


def test_maybe_flush_only_at_threshold_and_clears_buffer():
    flushed = []

    class _DB:
        async def persist_quote_paths(self, rows):
            flushed.append(len(rows))
            return len(rows)

    db = _DB()

    async def _run():
        executor._quote_path_buffer.clear()
        for _ in range(10):                             # below threshold -> no flush, buffer kept
            executor._quote_path_buffer.append({"trade_id": "t"})
        executor._maybe_flush_quote_paths(db)
        assert flushed == [] and len(executor._quote_path_buffer) == 10

        executor._quote_path_buffer.clear()
        for _ in range(executor._QUOTE_PATH_FLUSH_AT):  # at threshold -> fire-and-forget flush
            executor._quote_path_buffer.append({"trade_id": "t"})
        executor._maybe_flush_quote_paths(db)
        assert len(executor._quote_path_buffer) == 0    # cleared immediately, without awaiting write
        await asyncio.sleep(0.02)                        # let the background write run
        assert flushed and flushed[-1] == executor._QUOTE_PATH_FLUSH_AT
    asyncio.run(_run())
    executor._quote_path_buffer.clear()
