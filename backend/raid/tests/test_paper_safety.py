"""B0 — paper-only fail-closed safety flags + guarded order boundary.

Verifies the three live flags default safe, parse fail-closed, and that the executor's
live-order stubs refuse to place unless an operator deliberately flips all three flags.
No network, no DB. Auto-discovered by raid.tests.run_all.
"""

import asyncio
import os

import config
import executor


def test_paper_only_flags_default_safe():
    assert config.PAPER_ONLY is True
    assert config.LIVE_TRADING_ENABLED is False
    assert config.KRAKEN_LIVE_ENABLED is False
    assert config.PAPER_MODE is True                  # existing structural gate still engaged
    assert config.live_orders_allowed() is False      # no live flag silently defaults true


def test_fail_closed_bool_unset_and_unparseable():
    key = "RAID_TEST_FLAG_B0"
    os.environ.pop(key, None)
    # unset -> the safe default is returned
    assert config._fail_closed_bool(key, safe_default=True) is True
    assert config._fail_closed_bool(key, safe_default=False) is False
    try:
        os.environ[key] = "banana"                    # unparseable -> safe default (fail-closed)
        assert config._fail_closed_bool(key, safe_default=True) is True
        assert config._fail_closed_bool(key, safe_default=False) is False
        os.environ[key] = "TRUE"                       # explicit true parses
        assert config._fail_closed_bool(key, safe_default=False) is True
        os.environ[key] = "0"                          # explicit false parses
        assert config._fail_closed_bool(key, safe_default=True) is False
    finally:
        os.environ.pop(key, None)


def test_live_orders_allowed_requires_all_three():
    saved = (config.PAPER_ONLY, config.LIVE_TRADING_ENABLED, config.KRAKEN_LIVE_ENABLED)
    try:
        config.PAPER_ONLY = False
        config.LIVE_TRADING_ENABLED = True
        config.KRAKEN_LIVE_ENABLED = True
        assert config.live_orders_allowed() is True    # only the deliberate triple-flip allows
        config.PAPER_ONLY = True
        assert config.live_orders_allowed() is False    # PAPER_ONLY still on -> forbid
        config.PAPER_ONLY = False
        config.KRAKEN_LIVE_ENABLED = False
        assert config.live_orders_allowed() is False    # a single live flag off -> forbid
        config.KRAKEN_LIVE_ENABLED = True
        config.LIVE_TRADING_ENABLED = False
        assert config.live_orders_allowed() is False    # the other live flag off -> forbid
    finally:
        config.PAPER_ONLY, config.LIVE_TRADING_ENABLED, config.KRAKEN_LIVE_ENABLED = saved


def test_order_boundary_guard_raises_by_default():
    raised = False
    try:
        executor._assert_live_orders_allowed("kraken")
    except RuntimeError:
        raised = True
    assert raised is True


def test_place_order_stubs_refuse_without_triple_flip():
    # Both live-order stubs must refuse (guard wired into each) with default safe flags.
    for coro in (executor._place_kraken_order(None, 100.0, 1.0),
                 executor._place_kalshi_order(None, 100.0, 1.0)):
        refused = False
        try:
            asyncio.run(coro)
        except RuntimeError:
            refused = True
        assert refused is True


def test_guard_passes_only_when_triple_flipped():
    saved = (config.PAPER_ONLY, config.LIVE_TRADING_ENABLED, config.KRAKEN_LIVE_ENABLED)
    try:
        config.PAPER_ONLY = False
        config.LIVE_TRADING_ENABLED = True
        config.KRAKEN_LIVE_ENABLED = True
        executor._assert_live_orders_allowed("kraken")   # must NOT raise when all three flipped
    finally:
        config.PAPER_ONLY, config.LIVE_TRADING_ENABLED, config.KRAKEN_LIVE_ENABLED = saved
