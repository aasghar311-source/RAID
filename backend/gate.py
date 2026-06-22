"""RAID gate — five risk checks every signal must pass before it can become a trade."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import config
from signals import Signal

log = logging.getLogger("raid.gate")


@dataclass
class GateResult:
    """Outcome of the risk gate: whether the signal passed and why."""

    passed: bool
    reason: str


async def check_gate(signal: Signal, db):
    """Run the five risk checks in order, returning on the first failure."""
    today = datetime.now(timezone.utc).date().isoformat()

    # CHECK 1 — kill switch.
    try:
        if await db.get_kill_switch():
            return GateResult(False, "kill_switch_active")
    except Exception as exc:  # noqa: BLE001
        log.error("gate kill-switch check failed: %s", exc)

    # CHECK 2 — daily loss limit.
    try:
        equity = await db.get_equity()
        daily_loss_limit = equity * config.DAILY_LOSS_LIMIT_PCT
        stats = await db.get_daily_stats(today)
        if stats and abs(min(stats.get("pnl", 0) or 0, 0)) >= daily_loss_limit:
            await db.set_kill_switch(
                True,
                f"Daily loss limit hit: ${abs(stats.get('pnl', 0)):.2f}",
                "gate_auto",
            )
            return GateResult(False, "daily_loss_limit")
    except Exception as exc:  # noqa: BLE001
        log.error("gate daily-loss check failed: %s", exc)

    # CHECK 3 — consecutive loss pause.
    try:
        consec = await db.get_consecutive_losses()
        if consec >= config.CONSECUTIVE_LOSS_PAUSE:
            return GateResult(False, f"consecutive_loss_pause_{consec}_losses")
    except Exception as exc:  # noqa: BLE001
        log.error("gate consecutive-loss check failed: %s", exc)

    # CHECK 4 — max open trades.
    try:
        open_trades = await db.get_open_trades()
        if len(open_trades) >= config.MAX_OPEN_TRADES:
            return GateResult(False, "max_open_trades")
    except Exception as exc:  # noqa: BLE001
        log.error("gate max-open check failed: %s", exc)

    # CHECK 5 — Kalshi slot limit.
    try:
        if signal.market == "kalshi":
            kalshi_trades = await db.get_open_trades_by_market("kalshi")
            if len(kalshi_trades) >= config.KALSHI_MAX_OPEN:
                return GateResult(False, "kalshi_max_open")
    except Exception as exc:  # noqa: BLE001
        log.error("gate kalshi-slot check failed: %s", exc)

    return GateResult(True, "all_checks_passed")
