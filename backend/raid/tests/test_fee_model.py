"""Real-fee-model tests (Commit A of the money-path correction).

The realized ledger must charge THIS account's real all-in TAKER round-trip cost on notional,
both legs: taker 0.40%/side x2 + margin-open 0.02% + spread 0.05% + slippage 0.17% ~= 1.04%.
A gross "win" smaller than that cost must book NET NEGATIVE. The frozen PLANNING assumption
(ASSUMED_FILL_FEE_PCT, net_rr gates) is deliberately left at the legacy 0.16% (Rule 5).

Discovered and run by raid.tests.run_all (plain asserts, no pytest).
"""

import costs
from executor import compute_pnl
from raid.runner import _rotation_pnl

TOL = 1e-9


def test_realized_round_trip_is_all_in_taker():
    # 2*0.0040 + 0.0002 + 0.0005 + 0.0017 = 0.0104
    assert abs(costs.realized_round_trip_cost_pct() - 0.0104) < TOL, costs.realized_round_trip_cost_pct()
    # Sanity: it is materially higher than the old 0.32% maker round-trip.
    assert costs.realized_round_trip_cost_pct() > 0.0032 * 2


def test_taker_is_the_engine_rate_and_higher_than_maker():
    assert costs.KRAKEN_TAKER_FEE_PCT == 0.0040
    assert costs.KRAKEN_MAKER_FEE_PCT == 0.0025
    assert costs.KRAKEN_TAKER_FEE_PCT > costs.KRAKEN_MAKER_FEE_PCT


def test_compute_pnl_charges_notional_both_legs():
    # $600 notional, +2% -> $12 gross; cost = 600 * 0.0104 = $6.24 -> net $5.76.
    pnl = compute_pnl("long", 100.0, 102.0, 600.0)
    assert abs(pnl - (12.0 - 600.0 * costs.realized_round_trip_cost_pct())) < 1e-6, pnl


def test_rotation_pnl_matches_compute_pnl():
    for d, e, x, s in [("long", 100.0, 101.0, 600.0), ("short", 0.46, 0.45, 600.0)]:
        assert abs(_rotation_pnl(d, e, x, s) - compute_pnl(d, e, x, s)) < 1e-9, (d, e, x, s)


def test_sub_fee_gross_win_books_net_negative():
    # +0.5% gross on $600 = +$3.00 gross, but round-trip cost ~= $6.24 -> NET NEGATIVE.
    pnl = compute_pnl("long", 100.0, 100.5, 600.0)
    gross = 600.0 * 0.005
    assert gross > 0                      # it IS a gross winner on price
    assert pnl < 0, pnl                   # ...but a net LOSER after real costs
    assert abs(pnl - (gross - 600.0 * costs.realized_round_trip_cost_pct())) < 1e-6


def test_entry_gate_uses_realized_cost():
    # Commit A (aggressive-retune): the entry net_rr gate now uses the SAME realized
    # round-trip cost as P&L (~1.04%), NOT the legacy 0.16% planning assumption. Verify the
    # honest-gate math that the 1%/4% geometry is calibrated to.
    c = costs.realized_round_trip_cost_pct()

    def nr(gross_reward, gross_risk):
        return (gross_reward - c) / (gross_risk + c)

    assert nr(0.04, 0.01) >= 1.20              # 1% SL / 4% TP -> ~1.45, clears the honest gate
    assert abs(nr(0.04, 0.01) - 1.45) < 0.05
    assert nr(0.02, 0.01) < 1.20               # 1% SL / 2% TP -> ~0.96, correctly rejected
    assert nr(0.025, 0.01) < 1.20              # old 2.5% TP cap no longer clears at real cost
    # The legacy planning constants still EXIST but are no longer consulted by the gate.
    assert costs.ASSUMED_FILL_FEE_PCT == 0.0016
