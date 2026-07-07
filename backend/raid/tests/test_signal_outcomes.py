"""B7 — signal-quality measurement (measure-only): build_signal_outcome_row correctness.

The row feeds NO decision — it is recorded per close (signal_outcomes) + logged (SIGNAL_OUTCOME) so
the per-strategy/regime/direction accuracy + R ledger can START on the OLD signal (Stage C baseline).
Pure helper tested directly. No DB. Auto-discovered by raid.tests.run_all.
"""

from db import build_signal_outcome_row


def _trade(**kw):
    base = {"entry_price": 100.0, "direction": "long", "size_usd": 600.0,
            "market_regime": "RANGE", "claude_reasoning": "RAID-C4 market net_rr=1.3 :: x",
            "initial_stop_distance_pct": 0.01, "peak_pnl_pct": 1.5, "mae_pct": -0.4,
            "entry_slope": 0.002}
    base.update(kw)
    return base


def test_long_win_direction_correct_and_net_r():
    row = build_signal_outcome_row(_trade(), "t1", 105.0, 30.0, "take_profit", 42.0)
    assert row["strategy_id"] == "RAID-C4" and row["direction"] == "long"
    assert row["regime_at_entry"] == "RANGE"
    assert row["realized_price_direction"] == "up" and row["direction_correct"] is True
    assert abs(row["realized_r"] - 30.0 / (600.0 * 0.01)) < 1e-9   # = 5.0 net-of-cost R
    assert row["entry_slope_direction"] == "up"
    assert row["net_pnl"] == 30.0 and row["mfe_pct"] == 1.5 and row["mae_pct"] == -0.4
    assert row["hold_minutes"] == 42.0 and row["close_reason"] == "take_profit"


def test_long_loss_direction_incorrect():
    row = build_signal_outcome_row(_trade(), "t2", 98.0, -15.0, "stop_loss", 30.0)
    assert row["realized_price_direction"] == "down" and row["direction_correct"] is False
    assert row["realized_r"] < 0


def test_short_win_direction_correct():
    row = build_signal_outcome_row(_trade(direction="short", claude_reasoning="RAID-C3 x", entry_slope=-0.001),
                                   "t3", 95.0, 25.0, "take_profit", 20.0)
    assert row["strategy_id"] == "RAID-C3" and row["direction"] == "short"
    assert row["realized_price_direction"] == "down" and row["direction_correct"] is True
    assert row["entry_slope_direction"] == "down"


def test_missing_stop_distance_gives_none_r():
    row = build_signal_outcome_row(_trade(initial_stop_distance_pct=None), "t4", 105.0, 30.0, "mat", 60.0)
    assert row["realized_r"] is None                    # can't compute R without the stop distance
    assert row["realized_price_direction"] == "up"       # direction still computable


def test_flat_move_and_missing_fields_are_safe():
    # entirely missing exit/pnl -> direction + R unknown, nothing fabricated
    row = build_signal_outcome_row({"direction": "long"}, "t5", None, None, "x", None)
    assert row["strategy_id"] is None and row["realized_price_direction"] is None
    assert row["direction_correct"] is None and row["realized_r"] is None
    # a flat (breakeven) close: not "correct" for a directional bet
    row2 = build_signal_outcome_row(_trade(), "t6", 100.0, 0.0, "breakeven_exit", 10.0)
    assert row2["realized_price_direction"] == "flat" and row2["direction_correct"] is False
