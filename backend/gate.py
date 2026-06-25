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

    # CHECK 3 — consecutive loss pause (auto-resumes after CONSECUTIVE_LOSS_PAUSE_MINUTES).
    try:
        consec = await db.get_consecutive_losses()
        if consec >= config.CONSECUTIVE_LOSS_PAUSE:
            last_loss = await db.get_last_loss_time()
            if last_loss is not None:
                elapsed_min = (datetime.now(timezone.utc) - last_loss).total_seconds() / 60
                if elapsed_min < config.CONSECUTIVE_LOSS_PAUSE_MINUTES:
                    return GateResult(False, f"consecutive_loss_pause_{consec}_losses")
            # last_loss is None (can't determine when) — fail open to avoid a
            # permanent block; the pause only holds within the 60-minute window.
    except Exception as exc:  # noqa: BLE001
        log.error("gate consecutive-loss check failed: %s", exc)

    # CHECK 4 — max open trades + 70% equity deployment cap.
    try:
        open_trades = await db.get_open_trades()
        if len(open_trades) >= config.MAX_OPEN_TRADES:
            return GateResult(False, "max_open_trades")
        # Deployment cap: never hold more than MAX_EQUITY_DEPLOYED_PCT of equity
        # in open positions. Protects the remainder as dry powder.
        try:
            deployed = sum(float(t.get("size_usd") or 0) for t in open_trades)
            equity_now = await db.get_equity()
            if equity_now > 0 and deployed >= equity_now * config.MAX_EQUITY_DEPLOYED_PCT:
                return GateResult(False, "max_equity_deployed")
        except Exception as exc:  # noqa: BLE001
            log.error("gate deployment-cap check failed: %s", exc)
        for t in open_trades:
            if t.get("symbol") == signal.symbol and t.get("direction") == signal.direction:
                return GateResult(False, f"duplicate_{signal.symbol}_{signal.direction}")
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
