"""Commit 1 instrumentation helpers — pure, no I/O. Plain asserts (run_all discovers)."""

from datetime import datetime, timezone

from raid.core.features import volume_ratio
from raid.execution.instrumentation import excursion_update, minutes_since

TOL = 1e-9


def _candle(vol):
    # [ts, open, high, low, close, volume]
    return [0, 1.0, 1.0, 1.0, 1.0, vol]


def test_volume_ratio_basic():
    rows = [_candle(10.0)] * 20 + [_candle(15.0)]     # 20 bars avg 10, latest 15
    assert abs(volume_ratio(rows) - 1.5) < TOL
    assert volume_ratio([_candle(10.0)] * 5) is None   # too few bars -> None (not 1.0)
    assert volume_ratio(None) is None
    zeros = [_candle(0.0)] * 20 + [_candle(5.0)]        # zero average -> None
    assert volume_ratio(zeros) is None


def test_minutes_since():
    now = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
    assert abs(minutes_since("2026-07-04T11:30:00+00:00", now=now) - 30.0) < TOL
    assert abs(minutes_since("2026-07-04T11:30:00Z", now=now) - 30.0) < TOL   # Z suffix
    assert minutes_since(None, now=now) == 0.0
    assert minutes_since("not-a-date", now=now) == 0.0
    # never negative (clock skew / future open_time)
    assert minutes_since("2026-07-04T12:30:00+00:00", now=now) == 0.0


def test_excursion_update_peak_rises_and_records_timing():
    # new high above the running peak -> peak + mfe timing written
    out = excursion_update(prev_peak=1.0, prev_mae=-0.5, cur_pct=2.5, minutes=12.0)
    assert out["peak_pnl_pct"] == 2.5 and out["mfe_minutes_from_entry"] == 12.0
    assert "mae_pct" not in out           # 2.5 is not a new low
    # not a new high -> no peak fields
    out2 = excursion_update(prev_peak=3.0, prev_mae=-0.5, cur_pct=1.0, minutes=20.0)
    assert "peak_pnl_pct" not in out2 and "mfe_minutes_from_entry" not in out2


def test_excursion_update_mae_falls_and_seeds():
    # new low -> mae + timing written
    out = excursion_update(prev_peak=1.0, prev_mae=-0.5, cur_pct=-1.2, minutes=8.0)
    assert out["mae_pct"] == -1.2 and out["mae_minutes_from_entry"] == 8.0
    # prev_mae None (first cycle) always seeds mae
    out2 = excursion_update(prev_peak=0, prev_mae=None, cur_pct=0.3, minutes=1.0)
    assert out2["mae_pct"] == 0.3 and out2["mae_minutes_from_entry"] == 1.0


def test_excursion_update_peak_floored_semantics_preserved():
    # caller passes prev_peak floored at 0; a losing-only trade never lifts peak_pnl_pct
    out = excursion_update(prev_peak=0, prev_mae=None, cur_pct=-0.4, minutes=3.0)
    assert "peak_pnl_pct" not in out          # peak stays at its 0 floor (old behavior)
    assert out["mae_pct"] == -0.4             # but MAE is captured


def test_excursion_update_no_change_returns_empty():
    assert excursion_update(prev_peak=3.0, prev_mae=-2.0, cur_pct=1.0, minutes=5.0) == {}
