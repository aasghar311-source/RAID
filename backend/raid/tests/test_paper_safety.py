"""B0 (+ amendment) — paper-only fail-closed safety flags + guarded order boundary.

Verifies the live flags default safe; parse fail-closed INCLUDING on a raising parser;
that live_orders_allowed() requires literal PAPER_MODE=False + PAPER_ONLY=False + both
live flags `is True` (no truthy-string/int coercion) and that PAPER_MODE=True alone
forces a block; and that the executor's live-order stubs refuse — including when the
flag check itself raises. No network, no DB. Auto-discovered by raid.tests.run_all.
"""

import asyncio
import os

import config
import executor


def _snapshot():
    return (config.PAPER_MODE, config.PAPER_ONLY,
            config.LIVE_TRADING_ENABLED, config.KRAKEN_LIVE_ENABLED)


def _restore(s):
    (config.PAPER_MODE, config.PAPER_ONLY,
     config.LIVE_TRADING_ENABLED, config.KRAKEN_LIVE_ENABLED) = s


def _set_all_live():
    """The ONLY combination that permits live orders (used to prove each gate binds)."""
    config.PAPER_MODE = False
    config.PAPER_ONLY = False
    config.LIVE_TRADING_ENABLED = True
    config.KRAKEN_LIVE_ENABLED = True


def test_paper_only_flags_default_safe():
    assert config.PAPER_ONLY is True
    assert config.LIVE_TRADING_ENABLED is False
    assert config.KRAKEN_LIVE_ENABLED is False
    assert config.PAPER_MODE is True                  # existing structural gate still engaged
    assert config.live_orders_allowed() is False      # no live flag silently defaults true


def test_fail_closed_bool_unset_and_unparseable():
    key = "RAID_TEST_FLAG_B0"
    os.environ.pop(key, None)
    assert config._fail_closed_bool(key, safe_default=True) is True
    assert config._fail_closed_bool(key, safe_default=False) is False
    try:
        os.environ[key] = "banana"                     # unparseable -> safe default
        assert config._fail_closed_bool(key, safe_default=True) is True
        assert config._fail_closed_bool(key, safe_default=False) is False
        os.environ[key] = "TRUE"                        # explicit true parses
        assert config._fail_closed_bool(key, safe_default=False) is True
        os.environ[key] = "0"                           # explicit false parses
        assert config._fail_closed_bool(key, safe_default=True) is False
    finally:
        os.environ.pop(key, None)


def test_fail_closed_bool_returns_safe_default_when_parser_raises():
    # (amendment a) If env access itself raises, the parser returns the SAFE default.
    saved = os.getenv
    try:
        def _boom(_name):
            raise RuntimeError("env access boom")
        os.getenv = _boom  # type: ignore[assignment]
        assert config._fail_closed_bool("ANYTHING", safe_default=False) is False
        assert config._fail_closed_bool("ANYTHING", safe_default=True) is True
    finally:
        os.getenv = saved  # type: ignore[assignment]


def test_live_orders_allowed_requires_all_four_gates():
    saved = _snapshot()
    try:
        _set_all_live()
        assert config.live_orders_allowed() is True         # the sole live-permitting combo
        config.PAPER_MODE = True                             # PAPER_MODE alone re-blocks
        assert config.live_orders_allowed() is False
        _set_all_live(); config.PAPER_ONLY = True
        assert config.live_orders_allowed() is False
        _set_all_live(); config.LIVE_TRADING_ENABLED = False
        assert config.live_orders_allowed() is False
        _set_all_live(); config.KRAKEN_LIVE_ENABLED = False
        assert config.live_orders_allowed() is False
    finally:
        _restore(saved)


def test_no_truthy_string_or_int_coercion():
    # (amendment) Truthy strings/ints must NOT satisfy the strict `is True` checks.
    saved = _snapshot()
    try:
        config.PAPER_MODE = False
        config.PAPER_ONLY = False
        config.LIVE_TRADING_ENABLED = "true"    # truthy string, not True
        config.KRAKEN_LIVE_ENABLED = "1"          # truthy string, not True
        assert config.live_orders_allowed() is False
        config.LIVE_TRADING_ENABLED = 1           # truthy int, not True
        config.KRAKEN_LIVE_ENABLED = True
        assert config.live_orders_allowed() is False
    finally:
        _restore(saved)


def test_order_boundary_guard_raises_by_default():
    raised = False
    try:
        executor._assert_live_orders_allowed("kraken")
    except RuntimeError:
        raised = True
    assert raised is True


def test_place_order_stubs_refuse_without_triple_flip():
    for coro in (executor._place_kraken_order(None, 100.0, 1.0),
                 executor._place_kalshi_order(None, 100.0, 1.0)):
        refused = False
        try:
            asyncio.run(coro)
        except RuntimeError:
            refused = True
        assert refused is True


def test_stubs_refuse_with_live_flags_true_but_paper_mode_true():
    # (amendment b) All three live flags set, but PAPER_MODE still True -> both refuse.
    saved = _snapshot()
    try:
        config.PAPER_MODE = True
        config.PAPER_ONLY = False
        config.LIVE_TRADING_ENABLED = True
        config.KRAKEN_LIVE_ENABLED = True
        for coro in (executor._place_kraken_order(None, 100.0, 1.0),
                     executor._place_kalshi_order(None, 100.0, 1.0)):
            refused = False
            try:
                asyncio.run(coro)
            except RuntimeError:
                refused = True
            assert refused is True
    finally:
        _restore(saved)


def test_guard_blocks_when_flag_check_raises():
    # (amendment a) If the flag check itself raises, the boundary still blocks.
    saved = config.live_orders_allowed
    try:
        def _boom():
            raise RuntimeError("flag check boom")
        config.live_orders_allowed = _boom  # type: ignore[assignment]
        raised = False
        try:
            executor._assert_live_orders_allowed("kraken")
        except RuntimeError:
            raised = True
        assert raised is True
    finally:
        config.live_orders_allowed = saved  # type: ignore[assignment]


def test_guard_passes_only_when_all_four_gates_open():
    saved = _snapshot()
    try:
        _set_all_live()
        executor._assert_live_orders_allowed("kraken")   # must NOT raise when all four gates open
    finally:
        _restore(saved)
