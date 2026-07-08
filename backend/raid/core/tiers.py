"""Appendix-C §3-§9 pair TIER classifier — SHADOW (C.7). Sorts each pair into
CORE / AGGRESSIVE / OPPORTUNISTIC / DISABLED from the §2 liquidity metrics. Pure; feeds NO gate yet
(C.8 enforces).

Thresholds are the operator's §5-§9 tables applied VERBATIM. A pair earns the BEST tier whose EVERY
threshold it meets; below OPPORTUNISTIC (or spread > the §3 universal 0.25% floor, or missing data,
or Kraken leverage unknown) -> DISABLED (§9). volume_ratio is NOT a tier criterion — it is A.2's
per-entry gate; tier is a standing-liquidity property (using it made tiers flap on 5m noise).

SHADOW (from the tier list) is the promotion/observation STATE (§14, Stage F): a newly-earned active
tier starts SHADOW until evidence promotes it. This classifier reports the EARNED liquidity tier;
nothing here trades.
"""

TIER_ORDER = ("CORE", "AGGRESSIVE", "OPPORTUNISTIC", "DISABLED")

UNIVERSAL_MAX_SPREAD_PCT = 0.0025          # §3 hard floor — nothing wider than 0.25% ever trades
# §17 per-tier leverage ceilings (measure-only here; enforced in sizing at Stage G/C.9).
TIER_MAX_LEVERAGE = {"CORE": 3.00, "AGGRESSIVE": 2.25, "OPPORTUNISTIC": 1.50}
# Reference order size for the §7-9 depth MULTIPLES (depth@Xbps >= mult x this). Operator-set to the
# real paper position notional: $4,000 account, <=5 open, 0.5-0.9% risk, up to 3x -> ~$400-1,200 typical
# notional; $800 is the mid. So CORE @10bps needs >= 10 x $800 = $8,000 of executable depth.
DEPTH_REF_USD = 800.0

# Operator §5-§9 tables, verbatim. min_* => metric >= value; max_* => metric <= value;
# *_mult => depth USD >= mult * DEPTH_REF_USD.
TIERS = {
    "CORE": {
        "min_dollar_vol_24h": 1_500_000.0, "min_dollar_vol_30d_median": 1_000_000.0,
        "min_dollar_vol_5m_median": 2_500.0, "min_trailing20_vol_usd": 2_000.0,
        "min_latest_5m_vol_usd": 1_500.0, "max_spread_pct": 0.0015, "max_slippage_p90": 0.0012,
        "min_depth_10bps_mult": 10.0, "min_depth_25bps_mult": 30.0,
        "max_zero_volume_rate": 0.03, "max_low_volume_rate": 0.20,
    },
    "AGGRESSIVE": {
        "min_dollar_vol_24h": 500_000.0, "min_dollar_vol_30d_median": 350_000.0,
        "min_dollar_vol_5m_median": 800.0, "min_trailing20_vol_usd": 650.0,
        "min_latest_5m_vol_usd": 500.0, "max_spread_pct": 0.0022, "max_slippage_p90": 0.0018,
        "min_depth_10bps_mult": 5.0, "min_depth_25bps_mult": 15.0,
        "max_zero_volume_rate": 0.05, "max_low_volume_rate": 0.30,
    },
    "OPPORTUNISTIC": {
        "min_dollar_vol_24h": 250_000.0, "min_dollar_vol_30d_median": 200_000.0,
        "min_dollar_vol_5m_median": 350.0, "min_trailing20_vol_usd": 300.0,
        "min_latest_5m_vol_usd": 250.0, "max_spread_pct": 0.0025, "max_slippage_p90": 0.0020,
        "min_depth_10bps_mult": 3.0, "min_depth_25bps_mult": 10.0,
        "max_zero_volume_rate": 0.07, "max_low_volume_rate": 0.35,
    },
}

_MIN_METRICS = {   # threshold key -> (metric key, §12 reason code) for metric >= threshold
    "min_dollar_vol_24h": ("dollar_vol_24h", "VOLUME_24H_TOO_LOW"),
    "min_dollar_vol_30d_median": ("dollar_vol_30d_median", "VOLUME_30D_MEDIAN_TOO_LOW"),
    "min_dollar_vol_5m_median": ("dollar_vol_5m_median", "VOLUME_5M_MEDIAN_TOO_LOW"),
    "min_trailing20_vol_usd": ("trailing20_vol_usd", "VOLUME_5M_MEDIAN_TOO_LOW"),
    "min_latest_5m_vol_usd": ("latest_5m_vol_usd", "LATEST_5M_VOLUME_TOO_LOW"),
}
_MAX_METRICS = {   # threshold key -> (metric key, §12 reason code) for metric <= threshold
    "max_spread_pct": ("spread_pct", "SPREAD_TOO_WIDE"),
    "max_slippage_p90": ("slippage_p90", "SLIPPAGE_TOO_HIGH"),
    "max_zero_volume_rate": ("zero_volume_rate", "ZERO_VOLUME_RATE_TOO_HIGH"),
    "max_low_volume_rate": ("low_volume_rate", "LOW_VOLUME_RATE_TOO_HIGH"),
}
_DEPTH_MULTS = {   # threshold key -> (metric key, §12 reason code) for USD >= mult * DEPTH_REF_USD
    "min_depth_10bps_mult": ("depth_10bps_usd", "DEPTH_TOO_LOW"),
    "min_depth_25bps_mult": ("depth_25bps_usd", "DEPTH_TOO_LOW"),
}


def check(metrics, thresholds):
    """Deduped §12 reason codes for every threshold in `thresholds` that `metrics` FAILS. A missing
    metric (None) fails closed. Empty list = passes all. Pure."""
    reasons = []
    for tkey, (mkey, code) in _MIN_METRICS.items():
        if tkey in thresholds:
            v = metrics.get(mkey)
            if v is None or v < thresholds[tkey]:
                reasons.append(code)
    for tkey, (mkey, code) in _MAX_METRICS.items():
        if tkey in thresholds:
            v = metrics.get(mkey)
            if v is None or v > thresholds[tkey]:
                reasons.append(code)
    for tkey, (mkey, code) in _DEPTH_MULTS.items():
        if tkey in thresholds:
            v = metrics.get(mkey)
            if v is None or v < thresholds[tkey] * DEPTH_REF_USD:
                reasons.append(code)
    return list(dict.fromkeys(reasons))


def classify_tier(metrics):
    """(tier, reasons) by LIQUIDITY only. §3 universal spread floor first (> 0.25% or missing ->
    DISABLED). Else the best active tier whose thresholds all pass (reasons=[]). Else DISABLED with
    the OPPORTUNISTIC-fail reasons. Does NOT consider leverage — see classify_pair."""
    sp = metrics.get("spread_pct")
    if sp is None:
        return "DISABLED", ["INCOMPLETE_DATA"]
    if sp > UNIVERSAL_MAX_SPREAD_PCT:
        return "DISABLED", ["SPREAD_TOO_WIDE"]
    for tier in ("CORE", "AGGRESSIVE", "OPPORTUNISTIC"):
        if not check(metrics, TIERS[tier]):
            return tier, []
    return "DISABLED", check(metrics, TIERS["OPPORTUNISTIC"])


def tradeable_leverage(tier, kraken_cap):
    """Tradeable leverage = STRICTER of the §17 tier cap and the Kraken per-pair cap. None if the
    tier has no cap (DISABLED) or the Kraken cap is unknown. Margin-eligibility does NOT set the tier."""
    tcap = TIER_MAX_LEVERAGE.get(tier)
    if tcap is None or kraken_cap is None:
        return None
    return min(tcap, float(kraken_cap))


def classify_pair(metrics, kraken_cap):
    """Full §9 result: (tier, reasons, leverage). Liquidity tier from classify_tier, then DISABLED if
    the Kraken leverage/margin is UNKNOWN (§9). leverage = tradeable_leverage for an active tier."""
    tier, reasons = classify_tier(metrics)
    if kraken_cap is None:
        return "DISABLED", list(dict.fromkeys(reasons + ["PAIR_LEVERAGE_UNKNOWN"])), None
    return tier, reasons, tradeable_leverage(tier, kraken_cap)
