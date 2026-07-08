"""Appendix-C §3-§9 pair TIER classifier — SHADOW (C.7). Sorts each pair into
CORE / AGGRESSIVE / OPPORTUNISTIC / SHADOW / DISABLED from the §2 liquidity metrics. Pure; feeds NO
gate yet (C.8 enforces). SHADOW: the runner logs the distribution; nothing trades off this.

Thresholds:
  * SPREAD caps are OPERATOR-CONFIRMED: CORE 0.15% / AGGRESSIVE 0.22% / OPPORTUNISTIC 0.25%, with a
    universal 0.25% hard floor per §3 (nothing wider ever trades).
  * The NON-SPREAD thresholds (30d/24h USD volume, depth, slippage, volume_ratio) are this repo's
    RECONSTRUCTION of the §4-§9 tables pending the exact numbers — clearly labelled, tuned in one
    place. Change these when the operator supplies the exact §4-§9 minimums.

Classification: DISABLED if any §3 universal minimum fails (fail-closed — a MISSING metric fails).
Else the BEST active tier (CORE>AGGRESSIVE>OPPORTUNISTIC) whose EVERY threshold passes. Else SHADOW
(passes universal, earns no active tier -> observe only). Reason codes are the §12 machine-readable set.
"""

TIER_ORDER = ("CORE", "AGGRESSIVE", "OPPORTUNISTIC", "SHADOW", "DISABLED")

# §3 universal hard minimums — fail ANY -> DISABLED (never trades).
UNIVERSAL = {
    "max_spread_pct": 0.0025,               # operator-confirmed §3 floor
    "min_volume_ratio": 0.35,               # operator-confirmed §3 floor
    "min_dollar_vol_24h": 1_000_000.0,      # reconstruction
    "min_dollar_vol_30d_median": 1_000_000.0,   # reconstruction
    "min_depth_10bps_usd": 2_000.0,         # reconstruction
    "max_slippage_p50": 0.005,              # reconstruction (0.5%)
}
# Active tiers, tightest first. A pair earns the BEST whose EVERY threshold it meets.
TIERS = {
    "CORE":          {"max_spread_pct": 0.0015, "min_dollar_vol_30d_median": 50_000_000.0,
                      "min_depth_10bps_usd": 50_000.0, "min_volume_ratio": 0.50},
    "AGGRESSIVE":    {"max_spread_pct": 0.0022, "min_dollar_vol_30d_median": 10_000_000.0,
                      "min_depth_10bps_usd": 20_000.0, "min_volume_ratio": 0.40},
    "OPPORTUNISTIC": {"max_spread_pct": 0.0025, "min_dollar_vol_30d_median": 2_000_000.0,
                      "min_depth_10bps_usd": 5_000.0, "min_volume_ratio": 0.35},
}

# threshold key -> (metric key, §12 reason code) for a "metric >= threshold" minimum
_MINS = {
    "min_volume_ratio": ("volume_ratio", "VOLUME_RATIO_TOO_LOW"),
    "min_dollar_vol_24h": ("dollar_vol_24h", "VOLUME_24H_TOO_LOW"),
    "min_dollar_vol_30d_median": ("dollar_vol_30d_median", "VOLUME_30D_MEDIAN_TOO_LOW"),
    "min_depth_10bps_usd": ("depth_10bps_usd", "DEPTH_TOO_LOW"),
}


def check(metrics, thresholds):
    """Reason codes for every threshold in `thresholds` that `metrics` FAILS. A missing metric
    (None) fails closed. Empty list = passes all. Pure."""
    reasons = []
    if "max_spread_pct" in thresholds:
        sp = metrics.get("spread_pct")
        if sp is None or sp > thresholds["max_spread_pct"]:
            reasons.append("SPREAD_TOO_WIDE")
    if "max_slippage_p50" in thresholds:
        sl = metrics.get("slippage_p50")
        if sl is None or sl > thresholds["max_slippage_p50"]:
            reasons.append("SLIPPAGE_TOO_HIGH")
    for tkey, (mkey, code) in _MINS.items():
        if tkey in thresholds:
            v = metrics.get(mkey)
            if v is None or v < thresholds[tkey]:
                reasons.append(code)
    return reasons


def classify_tier(metrics):
    """(tier, reasons). DISABLED if any §3 universal minimum fails (reasons = which). Else the best
    active tier whose thresholds all pass (reasons = []). Else SHADOW (passes universal, earns no
    active tier; reasons = why it fell short of OPPORTUNISTIC)."""
    uni = check(metrics, UNIVERSAL)
    if uni:
        return "DISABLED", uni
    for tier in ("CORE", "AGGRESSIVE", "OPPORTUNISTIC"):
        if not check(metrics, TIERS[tier]):
            return tier, []
    return "SHADOW", check(metrics, TIERS["OPPORTUNISTIC"])
