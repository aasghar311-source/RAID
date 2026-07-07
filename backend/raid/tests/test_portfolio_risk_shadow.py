"""B6 measure-first — aggregate_open_risk correctly sizes real open risk (measurement only).

The runner LOGS this (PORTFOLIO_RISK_SHADOW) against the tier caps; it does NOT feed it into
risk.assess (no enforcement flip). Pure helper tested directly. No DB. Auto-discovered by
raid.tests.run_all.
"""

from raid.core.risk import RiskTier, TIER_LIMITS, aggregate_open_risk


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
    assert aggregate_open_risk([], 4000.0) == {"total": 0.0, "long": 0.0, "short": 0.0, "max_cluster": 0.0}
    assert aggregate_open_risk([{"symbol": "X"}], 4000.0)["total"] == 0.0    # missing entry/sl/size skipped
    assert aggregate_open_risk([_t("X", "long", 100, 99, 100)], 0.0)["total"] == 0.0   # zero equity safe
