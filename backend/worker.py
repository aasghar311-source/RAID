"""RAID worker v2 — Railway entrypoint.

Runs three CONCURRENT tasks so the exit monitor is never blocked by the brain:
  • _exit_monitor_loop  — executor.monitor_positions every 1s (NEVER change cadence)
                          + a fast emergency-close safety check every few seconds
  • _brain_loop         — full brain cycle every brain_cycle_minutes; honors kill/pause
  • _periodic_loop      — midnight reset, daily alerts, health-state refresh, auto-go-live
Plus a stdlib health server on HEALTH_CHECK_PORT.
"""

import asyncio
import json
import logging
import signal as signal_module
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import alert_manager
import brain
import config
import db
import executor
import scanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("raid.worker")
CDT = ZoneInfo("America/Chicago")

STATE = {
    "equity": config.STARTING_EQUITY,
    "daily_pnl": 0.0,
    "open_trades": 0,
    "kill_switch": False,
    "last_cycle": None,
    "trajectory_status": "ON_TRACK",
    "ai_spend_today": 0.0,
    "start_time": time.time(),
}

_shutdown = asyncio.Event()

# Ensures the emergency-close email fires once per activation, not every check.
_emergency_alerted = False

# How often the exit-monitor task re-reads operator_controls for the panic button.
EMERGENCY_CHECK_SECONDS = 3


# ── Emergency close (fast path, batched prices) ───────────────────────────

async def _handle_emergency_close(db_):
    """Flatten ALL open positions immediately, halt the bot, alert once."""
    global _emergency_alerted
    log.warning("OPERATOR: EMERGENCY CLOSE triggered")
    equity = await db_.get_equity()
    open_trades = await db_.get_open_trades()

    # Batch crypto prices in ONE Kraken call — per-trade fetches hit the rate limit
    # (the exact bug executor.monitor_positions was rewritten to avoid).
    crypto_symbols = [
        t["symbol"] for t in open_trades if t.get("market") == "crypto" and t.get("symbol")
    ]
    prices = await scanner.fetch_kraken_prices(crypto_symbols) if crypto_symbols else {}

    closed = 0
    for trade in open_trades:
        try:
            price = prices.get(trade.get("symbol"))
            if price is None and trade.get("market") == "kalshi":
                price = await scanner.fetch_kalshi_price(trade.get("symbol", ""))
            if price is None or price <= 0:
                price = trade.get("entry_price") or 0
                log.warning(
                    "EMERGENCY: no live price for %s — closing at entry %.6f (degraded pnl)",
                    trade.get("symbol"), price,
                )
            pnl = executor.compute_pnl(
                trade.get("direction"), trade.get("entry_price") or 0,
                price, trade.get("size_usd") or 0,
            )
            await db_.close_trade(trade["id"], price, pnl, "emergency_close")
            closed += 1
        except Exception as exc:  # noqa: BLE001
            log.error("Emergency close failed for %s: %s", trade.get("id"), exc)

    # Clear the flag + halt; verify the clear actually persisted.
    cleared = await db_.update_operator_controls({"emergency_close": False, "kill_switch": True})
    if not cleared:
        log.error("EMERGENCY: failed to clear emergency_close flag — needs manual DB fix")

    # One email per activation so a stuck flag cannot spam the operator.
    if not _emergency_alerted:
        await alert_manager.alert_emergency_close(equity, closed)
        _emergency_alerted = True

    log.warning("OPERATOR: EMERGENCY CLOSE complete — %d positions closed", closed)


# ── Exit monitor task (1-second cadence — NEVER change) ───────────────────

async def _exit_monitor_loop(db_):
    """Run the exit monitor every 1s; check the emergency button every few seconds.

    This is its OWN task so the brain cycle (scan + Claude latency) can never delay
    SL/TP/trailing/adverse-move exits.
    """
    global _emergency_alerted
    last_safety_check = 0.0
    while not _shutdown.is_set():
        try:
            now = time.time()

            # Fast safety check: honor the operator emergency-close panic button.
            if now - last_safety_check >= EMERGENCY_CHECK_SECONDS:
                controls = await db_.get_operator_controls()
                if controls.get("emergency_close"):
                    await _handle_emergency_close(db_)
                else:
                    _emergency_alerted = False  # reset once the flag is down
                last_safety_check = now

            # Exit monitor — every loop iteration (1s). NEVER change this cadence.
            await executor.monitor_positions(db_)
            if config.STOCKS_ENABLED or config.OPTIONS_ENABLED:
                await executor.close_eod_positions(db_)
        except Exception as exc:  # noqa: BLE001
            log.error("exit monitor loop error: %s", exc)
        await asyncio.sleep(config.LOOP_SLEEP_SECONDS)


# ── Brain entry gate (kill/pause honored at brain cadence) ────────────────

async def _brain_entry_gate(db_, controls: dict) -> bool:
    """Honor kill_switch / pause_entries before a brain cycle. Returns should_run.

    Emergency_close is handled by the exit-monitor task, not here (it must be
    near-immediate). kill/pause only affect ENTRIES, which only happen on the
    brain cadence — so checking them here is sufficient.
    """
    if controls.get("kill_switch"):
        log.info("OPERATOR: kill_switch active — no brain entries this cycle")
        await db_.log_regime({
            "market": "operator", "regime": "HALTED",
            "reasoning": "Operator kill switch active", "confidence": 1.0,
            "vol_30d": None, "trajectory": STATE.get("trajectory_status", "ON_TRACK"),
        })
        return False
    if controls.get("pause_entries"):
        log.info("OPERATOR: pause_entries active — monitoring only, no new trades")
        await db_.log_regime({
            "market": "operator", "regime": "PAUSED",
            "reasoning": "Operator pause_entries active", "confidence": 1.0,
            "vol_30d": None, "trajectory": STATE.get("trajectory_status", "ON_TRACK"),
        })
        return False
    return True


async def _run_brain_cycle(db_, controls: dict):
    """Scan → news enrichment → brain. Crypto only in Phase 1."""
    log.info("── Brain cycle start ──")
    try:
        if not controls.get("crypto_enabled", True):
            log.info("WORKER: crypto disabled by operator_controls — skipping")
            return

        scan_results = await scanner.scan_kraken()
        if not scan_results:
            log.warning("WORKER: scan_kraken returned no results")
            return

        symbols = [r.symbol for r in scan_results]
        news_by_symbol = await scanner.scan_news(symbols)
        for r in scan_results:
            info = news_by_symbol.get(r.symbol)
            if info:
                r.news_headline = info.get("headline")
                r.news_sentiment = info.get("sentiment", "neutral")
                r.news_published = info.get("published_at")

        await brain.run_brain_cycle(scan_results, news_by_symbol, db_, controls)
    except Exception as exc:  # noqa: BLE001
        log.error("_run_brain_cycle failed: %s", exc)


async def _brain_loop(db_):
    """Run a full brain cycle every brain_cycle_minutes (live from operator_controls)."""
    last_brain_cycle = 0.0
    last_learning = datetime.now(timezone.utc)
    while not _shutdown.is_set():
        try:
            now = time.time()
            controls = await db_.get_operator_controls()
            brain_cycle_secs = int(
                controls.get("brain_cycle_minutes") or config.BRAIN_CYCLE_MINUTES
            ) * 60

            if (now - last_brain_cycle) >= brain_cycle_secs:
                if await _brain_entry_gate(db_, controls):
                    await _run_brain_cycle(db_, controls)
                last_brain_cycle = now

            now_dt = datetime.now(timezone.utc)
            if (now_dt - last_learning) >= timedelta(days=config.LEARNING_INTERVAL_DAYS):
                await brain.run_weekly_learning(db_)
                last_learning = now_dt
        except Exception as exc:  # noqa: BLE001
            log.error("brain loop error: %s", exc)
        # Poll a few times a minute; the actual cycle gates on brain_cycle_secs.
        await asyncio.sleep(5)


# ── Daily alert checks ────────────────────────────────────────────────────

async def _check_daily_alerts(db_, controls: dict):
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        equity = await db_.get_equity()
        stats = await db_.get_daily_stats(today)
        daily_pnl = float(stats.get("pnl", 0) or 0) if stats else 0.0
        if equity > 0 and daily_pnl < 0:
            loss_pct = abs(daily_pnl) / equity
            threshold = float(controls.get("alert_on_loss_pct") or 0.05)
            if loss_pct >= threshold:
                await alert_manager.alert_daily_loss(equity, loss_pct, threshold)
        consec = await db_.get_consecutive_losses()
        if consec >= 3:
            await alert_manager.alert_consecutive_losses(consec, equity)
    except Exception as exc:  # noqa: BLE001
        log.error("_check_daily_alerts failed: %s", exc)


async def midnight_reset(db_):
    """Daily UTC reset: clear circuit breakers, snapshot equity, log yesterday."""
    try:
        brain.reset_daily_spend()

        record = await db_.get_kill_switch_record()
        if record and record.get("active") and "Daily loss" in (record.get("reason") or ""):
            await db_.set_kill_switch(False, "Midnight reset — new trading day", "worker_auto")

        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        stats = await db_.get_daily_stats(yesterday)
        pnl = float(stats.get("pnl", 0) or 0) if stats else 0.0
        trades = int(stats.get("total_trades", 0) or 0) if stats else 0
        win_rate = float(stats.get("win_rate", 0) or 0) if stats else 0.0

        equity = await db_.get_equity()
        await db_.update_equity(equity, pnl)

        log.info(
            "RAID DAILY RESET — %s — PnL: $%.2f — Trades: %d — Win Rate: %.0f%%",
            yesterday, pnl, trades, win_rate * 100,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("midnight_reset failed: %s", exc)


async def _periodic_loop(db_):
    """Midnight reset, daily alerts, health-state refresh, auto-go-live."""
    last_midnight_reset = datetime.now(timezone.utc).date()
    last_alert_check = 0.0
    while not _shutdown.is_set():
        try:
            now = time.time()
            now_dt = datetime.now(timezone.utc)

            if now_dt.date() != last_midnight_reset:
                await midnight_reset(db_)
                last_midnight_reset = now_dt.date()

            if (now - last_alert_check) >= 900:
                controls = await db_.get_operator_controls()
                await _check_daily_alerts(db_, controls)
                last_alert_check = now

            # Health-state refresh.
            try:
                STATE["equity"] = await db_.get_equity()
                open_trades = await db_.get_open_trades()
                STATE["open_trades"] = len(open_trades)
                STATE["kill_switch"] = await db_.get_kill_switch()
                STATE["ai_spend_today"] = brain.get_daily_spend()
                STATE["trajectory_status"] = brain.get_trajectory_status()
                STATE["last_cycle"] = now_dt.isoformat()

                today_str = now_dt.date().isoformat()
                if config.PAPER_MODE and today_str >= config.LIVE_DATE:
                    config.PAPER_MODE = False
                    log.warning("RAID GOING LIVE — %s", now_dt.isoformat())
            except Exception as exc:  # noqa: BLE001
                log.error("health state refresh failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.error("periodic loop error: %s", exc)
        await asyncio.sleep(15)


# ── Health endpoint ───────────────────────────────────────────────────────

def _health_payload():
    return {
        "status": "online",
        "bot": config.BOT_NAME,
        "mode": "paper" if config.PAPER_MODE else "live",
        "equity": STATE["equity"],
        "daily_pnl": STATE["daily_pnl"],
        "open_trades": STATE["open_trades"],
        "kill_switch": STATE["kill_switch"],
        "trajectory_status": STATE["trajectory_status"],
        "ai_spend_today": STATE["ai_spend_today"],
        "last_cycle": STATE["last_cycle"],
        "uptime_seconds": int(time.time() - STATE["start_time"]),
    }


async def _handle_health_conn(reader, writer):
    try:
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.LimitOverrunError):
            pass
        body = json.dumps(_health_payload()).encode("utf-8")
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        writer.write(response)
        await writer.drain()
    except Exception as exc:  # noqa: BLE001
        log.error("health request failed: %s", exc)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def start_health_server():
    server = await asyncio.start_server(
        _handle_health_conn, "0.0.0.0", config.HEALTH_CHECK_PORT
    )
    log.info("Health server listening on :%d", config.HEALTH_CHECK_PORT)
    return server


# ── Main ──────────────────────────────────────────────────────────────────

async def main():
    """Boot RAID and run the three concurrent loops until shutdown."""
    config.validate_config()

    try:
        await db.init()
        equity = await db.get_equity()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Supabase connection failed: {exc}") from exc

    await db.create_tables()

    log.info(
        "RAID ONLINE — %s — %s MODE — Equity: $%.2f",
        datetime.now(timezone.utc).isoformat(),
        "PAPER" if config.PAPER_MODE else "LIVE",
        equity,
    )

    server = await start_health_server()
    persisted = await db.get_spend_today()
    brain.set_daily_spend(persisted)

    tasks = [
        asyncio.create_task(_exit_monitor_loop(db), name="exit_monitor"),
        asyncio.create_task(_brain_loop(db), name="brain"),
        asyncio.create_task(_periodic_loop(db), name="periodic"),
    ]

    try:
        await _shutdown.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        server.close()
        try:
            await server.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        log.info("RAID OFFLINE — %s", datetime.now(timezone.utc).isoformat())


def _install_signal_handlers(loop):
    def _request_shutdown():
        log.info("Shutdown signal received.")
        _shutdown.set()

    for sig in (signal_module.SIGTERM, signal_module.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except (NotImplementedError, AttributeError):
            try:
                signal_module.signal(sig, lambda *_: _request_shutdown())
            except Exception:  # noqa: BLE001
                pass


def run():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


if __name__ == "__main__":
    run()
