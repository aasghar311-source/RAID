"""C.7 — Appendix-C §3-§9 pair tier classifier (SHADOW). Pure classify_tier tested directly:
universal §3 minimums -> DISABLED (a MISSING metric fails closed), the best-active-tier ladder,
and the SHADOW band (passes universal, earns no active tier). No DB. Run_all-discovered."""

from raid.core import tiers as T


def _m(**kw):
    """A metrics dict that earns CORE by default; override individual metrics to test each gate."""
    base = dict(symbol="X", spread_pct=0.0010, volume_ratio=0.9, dollar_vol_24h=100_000_000.0,
                dollar_vol_30d_median=100_000_000.0, depth_10bps_usd=200_000.0, slippage_p50=0.0005)
    base.update(kw)
    return base


def test_core_when_all_pass():
    tier, reasons = T.classify_tier(_m())
    assert tier == "CORE" and reasons == []


def test_disabled_on_universal_fail():
    assert T.classify_tier(_m(spread_pct=0.0030))[0] == "DISABLED"       # > 0.25% universal floor
    assert T.classify_tier(_m(volume_ratio=0.30))[0] == "DISABLED"       # < 0.35 universal floor
    t, r = T.classify_tier(_m(dollar_vol_30d_median=None))               # MISSING metric -> fail closed
    assert t == "DISABLED" and "VOLUME_30D_MEDIAN_TOO_LOW" in r


def test_tier_ladder():
    assert T.classify_tier(_m(spread_pct=0.0020, dollar_vol_30d_median=15_000_000.0,
                              depth_10bps_usd=25_000.0, volume_ratio=0.45))[0] == "AGGRESSIVE"
    assert T.classify_tier(_m(spread_pct=0.0024, dollar_vol_30d_median=3_000_000.0,
                              depth_10bps_usd=6_000.0, volume_ratio=0.36))[0] == "OPPORTUNISTIC"


def test_shadow_band():
    # passes universal (30d 1.5M >= 1M floor) but below OPPORTUNISTIC's 2M -> SHADOW, not DISABLED
    t, r = T.classify_tier(_m(spread_pct=0.0024, dollar_vol_30d_median=1_500_000.0,
                              depth_10bps_usd=6_000.0, volume_ratio=0.36))
    assert t == "SHADOW" and "VOLUME_30D_MEDIAN_TOO_LOW" in r
