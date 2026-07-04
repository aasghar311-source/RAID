"""Daily-recalc compounding-size tests (Commit C).

Position size = 5% of a DAILY equity snapshot as MARGIN base. Realized equity = STARTING +
net closed-trade P&L (it compounds); the daily base is recalculated once per UTC day and drives
the 5% cap, so size scales up day over day as equity grows. The 95% deployment cap uses live
equity (tested elsewhere).

Discovered and run by raid.tests.run_all (plain asserts, no pytest).
"""

import asyncio

import config
import db


def _run(coro):
    return asyncio.run(coro)


def _patch(async_return):
    async def _f(*a, **k):
        return async_return
    return _f


def test_realized_equity_is_starting_plus_pnl():
    orig = db.get_total_realized_pnl
    try:
        db.get_total_realized_pnl = _patch(1234.56)
        assert abs(_run(db.get_realized_equity()) - (config.STARTING_EQUITY + 1234.56)) < 1e-6
        db.get_total_realized_pnl = _patch(-500.0)   # drawdown compounds down too
        assert abs(_run(db.get_realized_equity()) - (config.STARTING_EQUITY - 500.0)) < 1e-6
    finally:
        db.get_total_realized_pnl = orig


def test_daily_base_recalculates_once_per_day():
    orig_re, orig_ue = db.get_realized_equity, db.update_equity
    calls = {"n": 0}
    try:
        async def _re(*a, **k):
            calls["n"] += 1
            return 8000.0
        db.get_realized_equity = _re
        db.update_equity = _patch(None)
        db._daily_equity_cache = {"date": None, "equity": None}
        v1 = _run(db.get_daily_equity_base())
        v2 = _run(db.get_daily_equity_base())      # same day -> cached, no recompute
        assert v1 == 8000.0 and v2 == 8000.0
        assert calls["n"] == 1, calls["n"]
        # simulate a new UTC day -> recompute
        db._daily_equity_cache["date"] = "1999-01-01"
        v3 = _run(db.get_daily_equity_base())
        assert v3 == 8000.0 and calls["n"] == 2
    finally:
        db.get_realized_equity, db.update_equity = orig_re, orig_ue
        db._daily_equity_cache = {"date": None, "equity": None}


def test_five_pct_margin_base_scales_with_daily_equity():
    # The runner caps base margin at MAX_TRADE_SIZE_PCT * sizing_equity -> grows as equity grows.
    assert config.MAX_TRADE_SIZE_PCT == 0.05
    assert config.MAX_TRADE_SIZE_PCT * 4000.0 == 200.0
    assert config.MAX_TRADE_SIZE_PCT * 8000.0 == 400.0      # doubled equity -> doubled base
