"""B5 — quote-path flight recorder: correct record + NON-BLOCKING flush (exit loop unaffected).

The per-tick capture is a synchronous buffer append (no I/O); the batched DB flush is fire-and-
forget so it can never block the 1s exit loop. No real DB, no network. Auto-discovered by
raid.tests.run_all.
"""

import asyncio
import logging

import executor


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


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


def test_raising_flush_is_caught_not_orphaned():
    # A flush coroutine that RAISES must be caught + logged (its exception retrieved), never left as
    # an orphaned unhandled-task warning, and the reference dropped. The exit loop is unaffected.
    async def _run():
        cap = _Capture()
        lg = logging.getLogger("raid.executor")
        lg.addHandler(cap)
        prev = lg.level
        lg.setLevel(logging.ERROR)
        try:
            async def boom():
                raise RuntimeError("flush boom")

            t = executor._spawn_flush(boom())
            await asyncio.sleep(0.02)                     # let the task run + the done-callback fire
            assert t.done()
            assert t not in executor._quote_flush_tasks    # reference dropped (not orphaned)
            assert any("quote-path flush failed" in m for m in cap.msgs), cap.msgs
        finally:
            lg.removeHandler(cap)
            lg.setLevel(prev)
    asyncio.run(_run())


def test_maybe_flush_survives_raising_persist():
    # If db.persist_quote_paths raises, _maybe_flush must NOT raise (fire-and-forget) and the buffer
    # is still cleared so the 1s loop continues; the error is caught in the task done-callback.
    class _DB:
        async def persist_quote_paths(self, rows):
            raise RuntimeError("db down")

    async def _run():
        db = _DB()
        executor._quote_path_buffer.clear()
        for _ in range(executor._QUOTE_PATH_FLUSH_AT):
            executor._quote_path_buffer.append({"trade_id": "t"})
        executor._maybe_flush_quote_paths(db)             # must NOT raise
        assert len(executor._quote_path_buffer) == 0       # cleared -> loop continues
        await asyncio.sleep(0.02)                           # let the raising task run + be caught
    asyncio.run(_run())
    executor._quote_path_buffer.clear()


def test_buffer_is_bounded_under_slow_db():
    # A slow/failing DB must never let the buffer grow without limit: _buffer_quote_path itself caps
    # at _QUOTE_PATH_MAX_BUFFER (ring-buffer), independent of whether a flush ever completes.
    executor._quote_path_buffer.clear()
    trade = {"id": "t", "symbol": "X", "direction": "long"}
    for _ in range(executor._QUOTE_PATH_MAX_BUFFER + 500):
        executor._buffer_quote_path(trade, 1.0, {"bid": 1.0, "ask": 2.0}, "bid", 0.0)
    assert len(executor._quote_path_buffer) <= executor._QUOTE_PATH_MAX_BUFFER
    executor._quote_path_buffer.clear()
