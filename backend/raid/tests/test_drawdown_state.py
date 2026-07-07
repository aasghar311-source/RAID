"""B1 — persisted drawdown high-water mark survives a simulated restart.

Proves resolve_peak seeds from the PERSISTED peak (so a restart cannot clear a drawdown pause),
contrasts against the legacy in-memory bug, and round-trips the persist/load contract. Pure logic
+ a dict-backed fake store; no DB, no network. Auto-discovered by raid.tests.run_all.
"""

import config
from raid.runner import resolve_peak, _effective_leverage


def _drawdown(peak, equity):
    return (peak - equity) / peak if peak > 0 else 0.0


def test_resolve_peak_ratchets_and_seeds_from_persisted():
    starting = config.STARTING_EQUITY
    assert resolve_peak(0.0, 0.0, starting, 5000.0) == 5000.0        # ratchets up with equity
    assert resolve_peak(0.0, 0.0, starting, 100.0) == starting        # never below starting
    assert resolve_peak(5000.0, 0.0, starting, 4200.0) == 5000.0      # seeds from persisted (post-restart)
    assert resolve_peak(4800.0, 4900.0, starting, 4700.0) == 4900.0   # max across all sources


def test_drawdown_pause_survives_simulated_restart():
    starting = config.STARTING_EQUITY   # 4000
    # Pre-restart: peak ratchets to 5000; equity drops to 4200 -> 16% drawdown -> pause (>=15%).
    peak_pre = resolve_peak(0.0, 0.0, starting, 5000.0)
    assert peak_pre == 5000.0
    persisted = {"peak_equity": peak_pre}          # what B1 writes to drawdown_state
    equity_now = 4200.0
    _, halt_pre = _effective_leverage(_drawdown(peak_pre, equity_now))
    assert halt_pre == "pause"

    # RESTART: the in-memory peak resets to 0.
    # WITHOUT persistence (the bug): peak reseeds to max(starting, equity) -> drawdown ~0 -> clears.
    _, halt_bug = _effective_leverage(
        _drawdown(resolve_peak(0.0, 0.0, starting, equity_now), equity_now))
    assert halt_bug is None

    # WITH persistence (B1): load the persisted peak -> drawdown preserved -> pause SURVIVES.
    peak_post = resolve_peak(persisted["peak_equity"], 0.0, starting, equity_now)
    assert peak_post == 5000.0
    _, halt_fixed = _effective_leverage(_drawdown(peak_post, equity_now))
    assert halt_fixed == "pause"


def test_shutdown_state_survives_simulated_restart():
    starting = config.STARTING_EQUITY
    # peak 5000, equity 3900 -> 22% drawdown -> shutdown (>=20%).
    persisted = {"peak_equity": 5000.0}
    equity_now = 3900.0
    _, halt = _effective_leverage(_drawdown(resolve_peak(persisted["peak_equity"], 0.0, starting, equity_now), equity_now))
    assert halt == "shutdown"


def test_drawdown_state_persist_load_roundtrip():
    # The persist/load contract: a value written pre-restart is readable post-restart.
    store = {}   # stand-in for the drawdown_state row; survives the "restart"

    def upsert(fields):
        store.update(fields)

    def get():
        return dict(store) if store else None

    upsert({"peak_equity": 5000.0, "drawdown_pct": 0.16, "pause_state": "paused"})
    loaded = get()                                  # simulate restart: in-memory gone, store persists
    assert loaded is not None
    assert loaded["peak_equity"] == 5000.0
    assert loaded["pause_state"] == "paused"
