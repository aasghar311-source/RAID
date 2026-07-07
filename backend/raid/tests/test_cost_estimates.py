"""B4 — versioned dynamic cost estimate (record-only) + cost_estimates persist contract.

The dynamic estimate is stamped with cost_model_version and floored at the realized 1.04%; it is
RECORDED per trade (cost_estimates) and does NOT replace the live gate/P&L cost. Pure + a fake
store round-trip. No DB, no network. Auto-discovered by raid.tests.run_all.
"""

import costs


def test_dynamic_cost_is_versioned_and_floored():
    est = costs.dynamic_round_trip_cost_pct()
    assert est["cost_model_version"] == costs.COST_MODEL_VERSION
    # default components reduce to (flat floor + uncertainty buffer); never below the floor
    assert est["total_pct"] >= costs.realized_round_trip_cost_pct() - 1e-12
    assert est["floor_pct"] == costs.realized_round_trip_cost_pct()
    for k in ("entry_fee_pct", "exit_fee_pct", "margin_open_pct", "spread_pct", "slippage_pct",
              "rollover_reserve_pct", "uncertainty_buffer_pct", "total_pct"):
        assert k in est


def test_dynamic_cost_overrides_and_rollover():
    wide = costs.dynamic_round_trip_cost_pct(spread_pct=0.01, slippage_pct=0.01)
    assert wide["spread_pct"] == 0.01 and wide["slippage_pct"] == 0.01
    assert wide["total_pct"] > costs.realized_round_trip_cost_pct()      # wider spread -> above floor
    assert costs.dynamic_round_trip_cost_pct(crosses_rollover=True)["rollover_reserve_pct"] == costs.ROLLOVER_RESERVE_PCT
    assert costs.dynamic_round_trip_cost_pct(crosses_rollover=False)["rollover_reserve_pct"] == 0.0


def test_dynamic_cost_does_not_change_live_cost():
    # The live gate/P&L cost is UNCHANGED by B4 (still the flat 1.04%).
    assert abs(costs.realized_round_trip_cost_pct() - 0.0104) < 1e-9


def test_cost_estimate_persist_roundtrip():
    store = []

    def insert(row):
        store.append(dict(row))

    est = costs.dynamic_round_trip_cost_pct()
    est.update({"trade_id": "t-123", "pair": "SOLUSD", "direction": "long"})
    insert(est)
    assert store and store[0]["trade_id"] == "t-123"
    assert store[0]["cost_model_version"] == costs.COST_MODEL_VERSION
    assert store[0]["pair"] == "SOLUSD"
