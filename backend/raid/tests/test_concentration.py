"""Concentration-cap tests (Phase-2 open-time gate against same-symbol stacking).

Reproduces the real SLXUSD RAID-C3-short 4-stack: four opens on consecutive 5-min cycles,
same (symbol,strategy,direction), all closed together for a ~-$20 loss cluster. With the cap
at 1 per (symbol,strategy,direction), only the first books; a third strategy on the same
symbol is blocked by the per-symbol total (2); unrelated symbols are unaffected.

Discovered and run by raid.tests.run_all (plain asserts, no pytest).
"""

from raid.core.universe import open_concentration_counts, concentration_reject_reason

MAX_SSD = 1
MAX_SYM = 2


def _open(symbol, strat, direction):
    # Trades carry the strategy id in claude_reasoning, matching the runner's booking tag.
    return {"symbol": symbol, "direction": direction,
            "claude_reasoning": f"{strat} limit net_rr=1.5 lev=3x margin=200.00 :: x"}


def test_counts_from_open_trades():
    open_trades = [
        _open("SLXUSD", "RAID-C3", "short"),
        _open("SLXUSD", "RAID-C3", "short"),
        _open("SLXUSD", "RAID-C7", "short"),
        _open("APTUSD", "RAID-C2", "long"),
    ]
    ssd, sym = open_concentration_counts(open_trades)
    assert ssd[("SLXUSD", "RAID-C3", "short")] == 2
    assert ssd[("SLXUSD", "RAID-C7", "short")] == 1
    assert sym["SLXUSD"] == 3
    assert sym["APTUSD"] == 1


def test_blocks_same_symbol_strategy_direction_stack():
    # One SLXUSD C3 short already open -> a second is rejected by the per-ssd cap (=1).
    ssd, sym = open_concentration_counts([_open("SLXUSD", "RAID-C3", "short")])
    reason = concentration_reject_reason(ssd, sym, "SLXUSD", "RAID-C3", "short", MAX_SSD, MAX_SYM)
    assert reason is not None and "per_symbol_strategy_direction" in reason


def test_four_stack_books_at_most_one():
    # Simulate the runner: start empty, "book" C3 shorts on SLXUSD across 4 cycles; only the
    # first should pass the gate (the increment is what the runner does after a successful book).
    ssd, sym = {}, {}
    booked = 0
    for _ in range(4):
        if concentration_reject_reason(ssd, sym, "SLXUSD", "RAID-C3", "short", MAX_SSD, MAX_SYM) is None:
            booked += 1
            sym["SLXUSD"] = sym.get("SLXUSD", 0) + 1
            ssd[("SLXUSD", "RAID-C3", "short")] = ssd.get(("SLXUSD", "RAID-C3", "short"), 0) + 1
    assert booked == 1, booked


def test_third_strategy_blocked_by_symbol_total():
    # Two different strategies already open on SLXUSD (long+short different strats) -> the
    # per-symbol total cap (2) blocks a third strategy regardless of its (symbol,strat,dir).
    ssd, sym = open_concentration_counts([
        _open("SLXUSD", "RAID-C3", "short"),
        _open("SLXUSD", "RAID-C1", "long"),
    ])
    reason = concentration_reject_reason(ssd, sym, "SLXUSD", "RAID-C2", "long", MAX_SSD, MAX_SYM)
    assert reason is not None and "per_symbol_total" in reason


def test_unrelated_symbol_unaffected():
    ssd, sym = open_concentration_counts([
        _open("SLXUSD", "RAID-C3", "short"),
        _open("SLXUSD", "RAID-C1", "long"),
    ])
    # A different symbol has no open positions -> not blocked.
    assert concentration_reject_reason(ssd, sym, "APTUSD", "RAID-C2", "long", MAX_SSD, MAX_SYM) is None


def test_same_symbol_different_direction_allowed_until_total():
    # Opposite direction is handled by has_opposite(); the per-ssd cap keys on direction, so a
    # long is allowed when only a short is open (until the per-symbol total is hit).
    ssd, sym = open_concentration_counts([_open("SLXUSD", "RAID-C3", "short")])
    assert concentration_reject_reason(ssd, sym, "SLXUSD", "RAID-C2", "long", MAX_SSD, MAX_SYM) is None
