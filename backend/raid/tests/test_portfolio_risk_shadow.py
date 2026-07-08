"""B6 measure-first — aggregate_open_risk correctly sizes real open risk (measurement only).

The runner LOGS this (PORTFOLIO_RISK_SHADOW) against the tier caps, and B.5 now also FEEDS it into
the booking loop (portfolio_cap_reason) to bind total/same-dir/cluster caps. Pure helpers tested
directly. No DB. Auto-discovered by raid.tests.run_all.
"""

from raid.core.risk import (
    RiskTier, TIER_LIMITS, aggregate_open_risk, portfolio_cap_reason, symbol_cluster_index,
)


def _t(symbol, direction, entry, sl, size):
    return {"symbol": symbol, "direction": direction, "entry_price": entry, "sl": sl, "size_usd": size}


def test_aggregate_open_risk_totals_and_directions():
    eq = 4000.0
    trades = [
        _t("SOLUSD", "long", 100.0, 99.0, 600.0),    # risk = 600 * 0.01 = 6.0
        _t("ETHUSD", "long", 100.0, 98.0, 600.0),    # risk = 600 * 0.02 = 12.0
        _t("XPLUSD", "short", 100.0, 101.0, 300.0),  # risk = 300 * 0.01 = 3.0
    ]
    agg = aggregate_open_risk(trades, eq, correlated_groups=None)
    assert abs(agg["total"] - (6.0 + 12.0 + 3.0) / eq) < 1e-9
    assert abs(agg["long"] - (6.0 + 12.0) / eq) < 1e-9
    assert abs(agg["short"] - 3.0 / eq) < 1e-9


def test_aggregate_open_risk_cluster_over_cap():
    eq = 4000.0
    groups = [["SOLUSD", "ETHUSD", "BTCUSD", "XRPUSD"], ["XLMUSD", "XMRUSD", "XDGUSD"]]
    trades = [
        _t("SOLUSD", "long", 100.0, 99.0, 4000.0),   # risk 40
        _t("ETHUSD", "long", 100.0, 99.0, 4000.0),   # risk 40 -> cluster0 = 80 -> 2.0% of 4000
    ]
    agg = aggregate_open_risk(trades, eq, groups)
    assert abs(agg["max_cluster"] - 80.0 / eq) < 1e-9
    # 2.0% cluster risk EXCEEDS the INITIAL tier cap of 1.5% — what the inert gate would block.
    assert agg["max_cluster"] > TIER_LIMITS[RiskTier.INITIAL].max_cluster_risk_pct


def test_aggregate_open_risk_skips_bad_and_empty():
    assert aggregate_open_risk([], 4000.0) == {
        "total": 0.0, "long": 0.0, "short": 0.0, "max_cluster": 0.0, "by_cluster": {}}
    assert aggregate_open_risk([{"symbol": "X"}], 4000.0)["total"] == 0.0    # missing entry/sl/size skipped
    assert aggregate_open_risk([_t("X", "long", 100, 99, 100)], 0.0)["total"] == 0.0   # zero equity safe


def test_aggregate_open_risk_by_cluster():
    eq = 4000.0
    groups = [["SOLUSD", "ETHUSD"], ["XLMUSD"]]
    trades = [_t("SOLUSD", "long", 100.0, 99.0, 4000.0), _t("ETHUSD", "long", 100.0, 99.0, 4000.0)]
    agg = aggregate_open_risk(trades, eq, groups)
    assert abs(agg["by_cluster"][0] - 80.0 / eq) < 1e-9   # cluster 0 = 40 + 40 = 80 -> 2.0%
    assert 1 not in agg["by_cluster"]                     # cluster 1 has no open trade


# --- B.5 binding-gate helpers ---
def test_symbol_cluster_index():
    groups = [["SOLUSD", "ETHUSD"], ["XLMUSD", "XMRUSD"]]
    assert symbol_cluster_index("ETHUSD", groups) == 0
    assert symbol_cluster_index("XMRUSD", groups) == 1
    assert symbol_cluster_index("NOPEUSD", groups) is None
    assert symbol_cluster_index("ETHUSD", None) is None


def _run(total=0.0, long=0.0, short=0.0, cluster=None):
    return {"total": total, "long": long, "short": short, "cluster": cluster or {}}


def test_portfolio_cap_reason_total():
    # 2.6% running + 0.5% candidate > 3.0% total cap -> blocked on total (checked first)
    assert portfolio_cap_reason(0.005, "long", None, _run(total=0.026),
                                max_total=0.03, max_same_dir=0.03, max_cluster=0.015) == "total"


def test_portfolio_cap_reason_same_dir_and_cluster():
    # fits total (0.5%+0.5%<3%) but same-dir short 1.4%+0.5%=1.9% > 1.5% same-dir cap
    assert portfolio_cap_reason(0.005, "short", None, _run(total=0.005, short=0.014),
                                max_total=0.03, max_same_dir=0.015, max_cluster=0.015) == "same_dir"
    # fits total + same-dir but cluster 0 at 1.2% + 0.5% = 1.7% > 1.5% cluster cap
    assert portfolio_cap_reason(0.005, "long", 0, _run(total=0.005, long=0.005, cluster={0: 0.012}),
                                max_total=0.03, max_same_dir=0.03, max_cluster=0.015) == "cluster"


def test_portfolio_cap_reason_fits_and_noise():
    assert portfolio_cap_reason(0.005, "long", 0, _run(total=0.005, cluster={0: 0.005}),
                                max_total=0.03, max_same_dir=0.03, max_cluster=0.015) is None
    assert portfolio_cap_reason(0.0, "long", 0, _run(total=0.99),   # cand_risk<=0 never blocks
                                max_total=0.03, max_same_dir=0.03, max_cluster=0.015) is None
