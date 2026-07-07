"""RAID gate — five risk checks every signal must pass before it can become a trade."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import config
from signals import Signal
from raid.core.universe import trade_margin

log = logging.getLogger("raid.gate")


@dataclass
class GateResult:
    """Outcome of the risk gate: whether the signal passed and why."""

    passed: bool
    reason: str


def _gate_failopen(check, signal, exc, swallowed, strategy=None, cycle_ts=None) -> None:
    """B0.5 measure-first instrumentation. Record a swallowed gate exception WITHOUT changing
    behavior (the caller still continues / fails open). Under a future fail-closed gate any such
    exception would REJECT the candidate. Each line carries the candidate reference (symbol +
    strategy + direction + cycle_ts) so a GATE_PASSED_ON_SWALLOW hit ties to its cycle/candidate and
    can correlate forward to a trade after booking — there is NO trade_id pre-booking. Greppable:
    GATE_FAILOPEN (per swallowed exception) + GATE_PASSED_ON_SWALLOW (entries that returned
    passed=True only because a check's exception was swallowed)."""
    try:
        swallowed.append(check)
        log.warning(
            "GATE_FAILOPEN check=%s symbol=%s market=%s strategy=%s direction=%s cycle_ts=%s "
            "would_reject_failclosed=1 exc=%s: %s",
            check, getattr(signal, "symbol", "?"), getattr(signal, "market", "?"),
            strategy or "?", getattr(signal, "direction", "?"), cycle_ts or "?",
            type(exc).__name__, exc,
        )
    except Exception:  # noqa: BLE001 — instrumentation must never affect the gate
        pass


async def check_gate(signal: Signal, db, *, strategy=None, cycle_ts=None):
    """Run the five risk checks in order, returning on the first failure.

    B0.5: swallowed exceptions are still swallowed (behavior UNCHANGED) but now instrumented via
    _gate_failopen, so a fail-closed flip can be sized from real logs before it is written. The
    optional `strategy` + `cycle_ts` (passed by the runner) tag each GATE_* line with the candidate
    reference (symbol+strategy+direction+cycle_ts) so a swallow ties to its cycle/candidate and can
    correlate forward to a trade after booking (there is no trade_id pre-booking)."""
    today = datetime.now(timezone.utc).date().isoformat()
    _swallowed: list[str] = []

    # CHECK 1 — kill switch.
    try:
        if await db.get_kill_switch():
            return GateResult(False, "kill_switch_active")
    except Exception as exc:  # noqa: BLE001
        _gate_failopen("kill_switch", signal, exc, _swallowed, strategy, cycle_ts)

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
        _gate_failopen("daily_loss", signal, exc, _swallowed, strategy, cycle_ts)

    # CHECK 4 — max open trades + 70% equity deployment cap.
    try:
        open_trades = await db.get_open_trades()
        if len(open_trades) >= config.MAX_OPEN_TRADES:
            return GateResult(False, "max_open_trades")
        # Deployment cap: never hold more than MAX_EQUITY_DEPLOYED_PCT of equity in open
        # positions. Counts MARGIN, not notional (leveraged trades tag 'margin=X'; legacy /
        # pre-leverage trades fall back to size_usd since notional==margin at 1x).
        try:
            deployed = sum(trade_margin(t) for t in open_trades)
            equity_now = await db.get_equity()
            if equity_now > 0 and deployed >= equity_now * config.MAX_EQUITY_DEPLOYED_PCT:
                return GateResult(False, "max_equity_deployed")
        except Exception as exc:  # noqa: BLE001
            _gate_failopen("deployment_cap", signal, exc, _swallowed, strategy, cycle_ts)
    except Exception as exc:  # noqa: BLE001
        _gate_failopen("max_open", signal, exc, _swallowed, strategy, cycle_ts)

    # CHECK 5 — Kalshi slot limit.
    try:
        if signal.market == "kalshi":
            kalshi_trades = await db.get_open_trades_by_market("kalshi")
            if len(kalshi_trades) >= config.KALSHI_MAX_OPEN:
                return GateResult(False, "kalshi_max_open")
    except Exception as exc:  # noqa: BLE001
        _gate_failopen("kalshi", signal, exc, _swallowed, strategy, cycle_ts)

    if _swallowed:
        try:
            log.warning(
                "GATE_PASSED_ON_SWALLOW symbol=%s market=%s strategy=%s direction=%s cycle_ts=%s "
                "checks=%s — under fail-closed this entry would REJECT",
                getattr(signal, "symbol", "?"), getattr(signal, "market", "?"), strategy or "?",
                getattr(signal, "direction", "?"), cycle_ts or "?", ",".join(_swallowed),
            )
        except Exception:  # noqa: BLE001
            pass
    return GateResult(True, "all_checks_passed")
