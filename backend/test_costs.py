"""Unit tests for backend/costs.py — dependency-free (run: python test_costs.py).

Also pytest-collectable (test_* functions). Verifies the cost model is arithmetically
correct AND drop-in compatible with the legacy executor.compute_pnl formula.
"""

import costs

TOL = 1e-9


def _legacy_compute_pnl(direction, entry, exit_price, size_usd):
    """Exact copy of executor.compute_pnl's formula for the compatibility assertion."""
    KRAKEN_TAKER_FEE_PCT = 0.0016
    fee_cost = size_usd * KRAKEN_TAKER_FEE_PCT * 2
    if direction in ("long", "yes"):
        gross = size_usd * (exit_price - entry) / entry
    else:
        gross = size_usd * (entry - exit_price) / entry
    return gross - fee_cost


def test_gross_long_and_short():
    # Long: +1% move on $100 = +$1 gross
    assert abs(costs.gross_pnl("long", 100.0, 101.0, 100.0) - 1.0) < TOL
    # Short: price falls 1% -> +$1 gross
    assert abs(costs.gross_pnl("short", 100.0, 99.0, 100.0) - 1.0) < TOL
    # Long loss
    assert abs(costs.gross_pnl("long", 100.0, 99.0, 100.0) - (-1.0)) < TOL


def test_net_pnl_matches_legacy():
    cases = [
        ("long", 100.0, 102.5, 100.0),
        ("long", 100.0, 99.0, 100.0),
        ("short", 1.7922, 1.7489, 99.45),
        ("short", 0.5412, 0.5257, 120.0),
        ("long", 74.62, 73.84, 99.0),
    ]
    for d, e, x, s in cases:
        assert abs(costs.net_pnl(d, e, x, s) - _legacy_compute_pnl(d, e, x, s)) < TOL, (d, e, x, s)


def test_breakdown_itemization():
    b = costs.compute_costs("long", 100.0, 102.5, 100.0)
    # gross +2.5, two maker fees of $0.16 each, no spread/slippage/financing
    assert abs(b.gross_pnl - 2.5) < TOL
    assert abs(b.entry_fee - 0.16) < TOL
    assert abs(b.exit_fee - 0.16) < TOL
    assert abs(b.spread_cost) < TOL
    assert abs(b.total_cost - 0.32) < TOL
    assert abs(b.net_pnl - (2.5 - 0.32)) < TOL
    d = b.as_dict()
    assert abs(d["net_pnl"] - b.net_pnl) < TOL


def test_spread_slippage_financing_added():
    b = costs.compute_costs(
        "long", 100.0, 102.5, 100.0,
        spread_pct=0.0005, slippage_pct=0.001, financing_cost=0.25,
    )
    # extra costs: spread 0.05 + slippage 0.10 + financing 0.25 = 0.40 on top of 0.32 fees
    assert abs(b.total_cost - (0.32 + 0.05 + 0.10 + 0.25)) < TOL
    assert abs(b.net_pnl - (2.5 - 0.72)) < TOL


def test_taker_rate_higher_than_maker():
    maker = costs.net_pnl("long", 100.0, 102.5, 100.0, fee_pct=costs.KRAKEN_MAKER_FEE_PCT)
    taker = costs.net_pnl("long", 100.0, 102.5, 100.0, fee_pct=costs.KRAKEN_TAKER_FEE_PCT)
    assert taker < maker  # taker fees eat more
    assert costs.KRAKEN_TAKER_FEE_PCT > costs.KRAKEN_MAKER_FEE_PCT


def test_net_rr_current_geometry():
    # Fixed 1.0% SL, 2.5% TP (current config). Net R:R at maker fees.
    rr = costs.net_rr(100.0, 99.0, 102.5)
    # net_reward = 0.025 - 0.0032 = 0.0218 ; net_risk = 0.01 + 0.0032 = 0.0132
    assert abs(rr - (0.0218 / 0.0132)) < 1e-6
    assert 1.6 < rr < 1.7  # ~1.65, well above the 1.25 gate


def test_net_rr_rejects_uneconomic():
    # Tiny target that costs eat entirely -> net reward negative, ratio < 0
    rr = costs.net_rr(100.0, 99.5, 100.2)  # 0.2% target, 0.32% round-trip cost
    assert rr is not None
    assert rr < 0  # net reward is negative -> caller must reject, not widen


def test_fail_closed_on_bad_entry():
    for fn in (
        lambda: costs.gross_pnl("long", 0.0, 100.0, 100.0),
        lambda: costs.net_pnl("long", -5.0, 100.0, 100.0),
        lambda: costs.net_rr(0.0, 99.0, 101.0),
    ):
        try:
            fn()
            raise AssertionError("expected ValueError on non-positive entry")
        except ValueError:
            pass


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        print(f"  PASS {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} tests passed")


if __name__ == "__main__":
    _run()
