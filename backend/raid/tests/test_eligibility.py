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
    for s in REMOVED:
        assert s not in config.PRIORITY_PAIRS, s
    assert len(config.PRIORITY_PAIRS) == 18, len(config.PRIORITY_PAIRS)


def test_removed_pairs_not_margin_eligible_and_fail_closed():
    for s in REMOVED:
        assert kraken_max_leverage(s) is None, s
        assert is_margin_eligible(s) is False, s
        assert capped_leverage(3, s) == 0, s          # 0 -> caller skips (fail closed)


def test_all_18_eligible_pairs_support_at_least_3x():
    assert len(config.KRAKEN_MAX_LEVERAGE) == 18
    for s in config.PRIORITY_PAIRS:
        assert is_margin_eligible(s), s
        assert kraken_max_leverage(s) >= 3, (s, kraken_max_leverage(s))


def test_effective_leverage_never_exceeds_kraken_cap():
    assert capped_leverage(3, "SOLUSD") == 3     # cap 10, target 3 -> 3
    assert capped_leverage(5, "APTUSD") == 4     # cap 4  < target 5 -> 4
    assert capped_leverage(3, "WIFUSD") == 3     # cap 3, target 3 -> 3


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
