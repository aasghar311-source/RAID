"""Per-pair eligibility + leverage-cap tests (Commit B of the money-path correction).

The 6 Kraken spot-only / sub-3x pairs are removed from the universe; the remaining 18 all
support >= 3x margin. A pair absent from the leverage map is NOT tradeable (fail closed), a
spot-only pair cannot be shorted/leveraged, and effective leverage never exceeds the Kraken cap.

Discovered and run by raid.tests.run_all (plain asserts, no pytest).
"""

import config
from raid.core.universe import kraken_max_leverage, is_margin_eligible, capped_leverage
from raid.core.provider import CAP_SPOT_LONG, CAP_SHORT, CAP_MARGIN
from raid.runner import _capabilities_for

REMOVED = ["SLXUSD", "SYNUSD", "GWEIUSD", "RAVEUSD", "PENDLEUSD", "FILUSD"]


def test_removed_pairs_not_in_universe():
    # The old spot-only / sub-3x removals stay out; universe is the 40 ATR set + TRX + BTC (C.7).
    for s in REMOVED:
        assert s not in config.PRIORITY_PAIRS, s
    assert len(config.PRIORITY_PAIRS) == 42, len(config.PRIORITY_PAIRS)


def test_removed_pairs_not_margin_eligible_and_fail_closed():
    for s in REMOVED:
        assert kraken_max_leverage(s) is None, s
        assert is_margin_eligible(s) is False, s
        assert capped_leverage(3, s) == 0, s          # 0 -> caller skips (fail closed)


def test_all_universe_pairs_margin_eligible_and_mapped():
    # Every priority pair must be in the leverage map and margin-eligible (>=2x). XLMUSD caps
    # at 2x; the rest >=3x. No spot-only pair may be in the universe. 42 = 40 + TRX + BTC (C.7).
    assert len(config.KRAKEN_MAX_LEVERAGE) == 42
    assert set(config.PRIORITY_PAIRS) == set(config.KRAKEN_MAX_LEVERAGE)
    for s in config.PRIORITY_PAIRS:
        assert is_margin_eligible(s), s
        assert kraken_max_leverage(s) >= 2, (s, kraken_max_leverage(s))


def test_effective_leverage_never_exceeds_kraken_cap():
    assert capped_leverage(3, "SOLUSD") == 3     # cap 10, target 3 -> 3
    assert capped_leverage(3, "SPXUSD") == 3     # cap 3, target 3 -> 3
    assert capped_leverage(3, "XLMUSD") == 2     # cap 2 < target 3 -> 2 (live 2x pair)
    assert capped_leverage(5, "PEPEUSD") == 5    # cap 5, target 5 -> 5


def test_two_x_pair_caps_at_two():
    # No live 2x pair remains (FIL removed); inject one to prove min(target, cap) at 2x.
    config.KRAKEN_MAX_LEVERAGE["TESTX2USD"] = 2
    try:
        assert capped_leverage(3, "TESTX2USD") == 2
        assert is_margin_eligible("TESTX2USD") is True
    finally:
        del config.KRAKEN_MAX_LEVERAGE["TESTX2USD"]


def test_spot_only_pair_cannot_short_or_leverage():
    caps = _capabilities_for("SLXUSD")            # removed / spot-only
    assert CAP_SPOT_LONG in caps
    assert CAP_SHORT not in caps
    assert CAP_MARGIN not in caps
    assert capped_leverage(3, "SLXUSD") == 0


def test_eligible_pair_gets_full_margin_caps():
    caps = _capabilities_for("SOLUSD")
    assert CAP_SPOT_LONG in caps
    assert CAP_SHORT in caps
    assert CAP_MARGIN in caps
