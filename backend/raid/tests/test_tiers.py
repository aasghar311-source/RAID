"""C.7 — Appendix-C §3-§9 pair tier classifier (SHADOW), exact operator thresholds. Pure functions
tested directly: universal §3 spread floor, fail-closed on missing metrics, the CORE/AGGRESSIVE/
OPPORTUNISTIC/DISABLED ladder, depth-as-multiple, zero/low-vol caps, §17 tradeable leverage, and the
§9 leverage-unknown DISABLE. No DB. Run_all-discovered."""

from raid.core import tiers as T


def _m(**kw):
    """A metrics dict that earns CORE by default; override individual metrics to test each gate."""
    base = dict(symbol="X", dollar_vol_24h=100e6, dollar_vol_30d_median=100e6,
                dollar_vol_5m_median=50_000.0, trailing20_vol_usd=40_000.0, latest_5m_vol_usd=30_000.0,
                spread_pct=0.0010, slippage_p90=0.0005, depth_10bps_usd=100_000.0,
                depth_25bps_usd=300_000.0, zero_volume_rate=0.0, low_volume_rate=0.0)
    base.update(kw)
    return base


def test_core_when_all_pass():
    assert T.classify_tier(_m()) == ("CORE", [])


def test_universal_spread_floor():
    assert T.classify_tier(_m(spread_pct=0.0030))[0] == "DISABLED"           # > 0.25% §3 floor
    t, r = T.classify_tier(_m(spread_pct=None))
    assert t == "DISABLED" and "INCOMPLETE_DATA" in r                        # missing spread -> closed


def test_missing_metric_fails_closed():
    t, r = T.classify_tier(_m(dollar_vol_30d_median=None))
    assert t == "DISABLED" and "VOLUME_30D_MEDIAN_TOO_LOW" in r


def test_ladder_by_spread():
    assert T.classify_tier(_m(spread_pct=0.0020))[0] == "AGGRESSIVE"         # >0.15 fail CORE, <=0.22
    assert T.classify_tier(_m(spread_pct=0.0024))[0] == "OPPORTUNISTIC"      # >0.22 fail AGG, <=0.25


def test_depth_multiple():
    ref = T.DEPTH_REF_USD                                    # track the constant, don't hardcode
    # depth below even OPPORTUNISTIC (3x @10bps) -> DISABLED with DEPTH_TOO_LOW
    t, r = T.classify_tier(_m(depth_10bps_usd=1.0, depth_25bps_usd=1.0))
    assert t == "DISABLED" and "DEPTH_TOO_LOW" in r
    # depth exactly at AGGRESSIVE band (5x @10bps, 15x @25bps) + AGG spread -> AGGRESSIVE
    assert T.classify_tier(_m(spread_pct=0.0020, depth_10bps_usd=5 * ref,
                              depth_25bps_usd=15 * ref))[0] == "AGGRESSIVE"


def test_zero_and_low_vol_caps():
    # recalibrated caps: CORE zero<=0.10, OPP (loosest) low<=0.75.
    assert T.classify_tier(_m(zero_volume_rate=0.15))[0] != "CORE"           # >10% zero-vol fails CORE
    t, r = T.classify_tier(_m(low_volume_rate=0.80))                         # >75% low-vol fails all tiers
    assert t == "DISABLED" and "LOW_VOLUME_RATE_TOO_HIGH" in r


def test_latest_5m_not_a_tier_criterion():
    # latest_5m_volume moved to a per-entry gate — a thin latest bar no longer changes the tier
    assert T.classify_tier(_m(latest_5m_vol_usd=10.0)) == ("CORE", [])
    assert T.classify_tier(_m(latest_5m_vol_usd=None)) == ("CORE", [])


def test_tier_gate():
    assert T.tier_gate("CORE", 0.0010) == (True, "OK")             # within CORE 0.15% cap
    assert T.tier_gate("CORE", 0.0020)[0] is False                 # 0.20% > CORE cap -> reject
    assert T.tier_gate("AGGRESSIVE", 0.0020) == (True, "OK")       # within AGG 0.22% cap
    assert T.tier_gate("OPPORTUNISTIC", 0.0024) == (True, "OK")    # within OPP 0.25% cap
    assert T.tier_gate("DISABLED", 0.0001)[0] is False             # not an active tier -> reject
    assert T.tier_gate("CORE", None)[0] is False                   # unknown spread -> reject


def test_leverage_and_classify_pair():
    assert T.tradeable_leverage("CORE", 10) == 3.00                          # min(3.00, 10)
    assert T.tradeable_leverage("OPPORTUNISTIC", 2) == 1.50                  # min(1.50, 2)
    assert T.tradeable_leverage("DISABLED", 10) is None
    t, r, lev = T.classify_pair(_m(), kraken_cap=None)                       # §9: leverage unknown
    assert t == "DISABLED" and "PAIR_LEVERAGE_UNKNOWN" in r and lev is None
    t2, _r2, lev2 = T.classify_pair(_m(), kraken_cap=10)
    assert t2 == "CORE" and lev2 == 3.00                                     # margin at 10x != upgrade
