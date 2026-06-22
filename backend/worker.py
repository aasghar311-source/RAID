"""RAID worker — the Railway entrypoint: startup checks, health server, and the main loop."""

import asyncio
import logging
import signal as signal_module
import time
from datetime import datetime, timedelta, timezone

from aiohttp import web

import config
import db
import scanner
import signals as signals_mod
import gate
import brain
import executor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("raid.worker")

# Shared runtime state surfaced by the health endpoint.
STATE = {
    "equity": config.STARTING_EQUITY,
    "daily_pnl": 0.0,
    "open_trades": 0,
    "kill_switch": False,
    "last_cycle": None,
    "start_time": time.time(),
}

_shutdown = asyncio.Event()


async def get_portfolio_summary(database):
    """Return a portfolio snapshot used by the brain and health endpoint."""
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        open_trades = await database.get_open_trades()
        stats = await database.get_daily_stats(today)
        consec = await database.get_consecutive_losses()
        macro_imminent, macro_name, mins = scanner.check_macro_events()
        macro_status = f"{macro_name} in {mins}m" if macro_imminent else "clear"
        return {
            "open_count": len(open_trades),
            "daily_pnl": float(stats.get("pnl", 0) or 0) if stats else 0.0,
            "win_rate": float(stats.get("win_rate", 0) or 0) if stats else 0.0,
            "consecutive_losses": consec,
            "macro_status": macro_status,
        }
    except Exception as exc:  # noqa: BLE001
        log.error("get_portfolio_summary failed: %s", exc)
        return {
            "open_count": 0,
            "daily_pnl": 0.0,
            "win_rate": 0.0,
            "consecutive_losses": 0,
            "macro_status": "unknown",
        }


async def _handle_signal_pipeline(signal, database, summary):
    """Run one signal through gate → brain → executor, logging the full path."""
    try:
        signal_row = {
            "market": signal.market,
            "symbol": signal.symbol,
            "direction": signal.direction,
            "confidence": signal.confidence,
            "news_sentiment": signal.news_sentiment,
            "technical_score": signal.technical_score,
            "ai_validated": False,
            "ai_decision": None,
            "rejected_reason": signal.block_reason or None,
            "entered_trade": False,
        }
        signal_id = await database.log_signal(signal_row)
        setattr(signal, "_signal_id", signal_id)

        if signal.macro_blocked:
            log.info("SIGNAL BLOCKED %s %s reason=%s", signal.market, signal.symbol, signal.block_reason)
            return

        gate_result = await gate.check_gate(signal, database)
        if not gate_result.passed:
            await database.update_signal(signal_id, {"rejected_reason": gate_result.reason})
            log.info("GATE REJECT %s %s reason=%s", signal.market, signal.symbol, gate_result.reason)
            return

        brain_result = await brain.validate_signal(signal, database, summary)
        await database.update_signal(
            signal_id,
            {"ai_validated": True, "ai_decision": brain_result.decision},
        )
        log.info(
            "BRAIN %s %s decision=%s conf=%.2f cost=$%.4f reason=%s",
            signal.market,
            signal.symbol,
            brain_result.decision,
            brain_result.confidence,
            brain_result.cost_usd,
            brain_result.reasoning,
        )

        if brain_result.decision != "ENTER":
            return

        trade_result = await executor.execute_trade(signal, brain_result, database)
        if trade_result.trade_id:
            await database.update_signal(signal_id, {"entered_trade": True})
        log.info(
            "EXEC %s %s status=%s id=%s size=$%.2f",
            signal.market,
            signal.symbol,
            trade_result.status,
            trade_result.trade_id,
            trade_result.size_usd,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("signal pipeline failed for %s: %s", getattr(signal, "symbol", "?"), exc)


async def _run_market(market: str, scan_fn, database):
    """Scan one market, enrich with news, generate signals, and process each."""
    try:
        results = await scan_fn()
        if not results:
            return
        if market == "crypto":
            news = await scanner.scan_news([r.symbol for r in results])
            for r in results:
                info = news.get(r.symbol)
                if info:
                    r.news_headline = info["headline"]
                    r.news_sentiment = info["sentiment"]
                    r.news_published = info["published_at"]

        market_signals = await signals_mod.generate_signals(results)
        summary = await get_portfolio_summary(database)
        for sig in market_signals:
            await _handle_signal_pipeline(sig, database, summary)
    except Exception as exc:  # noqa: BLE001
        log.error("_run_market(%s) failed: %s", market, exc)


async def midnight_reset(database):
    """Daily UTC reset: clear circuit breakers, snapshot equity, log yesterday's report."""
    try:
        brain.reset_daily_spend()

        # Deactivate a daily-loss-triggered kill switch.
        record = await database.get_kill_switch_record()
        if record and record.get("active") and "Daily loss" in (record.get("reason") or ""):
            await database.set_kill_switch(False, "Midnight reset — new trading day", "worker_auto")

        # Daily report from yesterday's stats.
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        stats = await database.get_daily_stats(yesterday)
        pnl = float(stats.get("pnl", 0) or 0) if stats else 0.0
        trades = int(stats.get("total_trades", 0) or 0) if stats else 0
        win_rate = float(stats.get("win_rate", 0) or 0) if stats else 0.0

        equity = await database.get_equity()
        await database.update_equity(equity, pnl)

        log.info(
            "RAID DAILY RESET — %s — PnL: $%.2f — Trades: %d — Win Rate: %.0f%%",
            yesterday,
            pnl,
            trades,
            win_rate * 100,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("midnight_reset failed: %s", exc)


async def health_handler(request):
    """Return the bot's live status as JSON for health checks."""
    return web.json_response(
        {
            "status": "online",
            "bot": config.BOT_NAME,
            "mode": "paper" if config.PAPER_MODE else "live",
            "equity": STATE["equity"],
            "daily_pnl": STATE["daily_pnl"],
            "open_trades": STATE["open_trades"],
            "kill_switch": STATE["kill_switch"],
            "last_cycle": STATE["last_cycle"],
            "uptime_seconds": int(time.time() - STATE["start_time"]),
            "paper_mode": config.PAPER_MODE,
        }
    )


async def start_health_server():
    """Start the aiohttp health server on HEALTH_CHECK_PORT and return its runner."""
    app = web.Application()
    app.router.add_get("/", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.HEALTH_CHECK_PORT)
    await site.start()
    log.info("Health server listening on :%d", config.HEALTH_CHECK_PORT)
    return runner


async def main():
    """Run RAID forever: scan, signal, gate, validate, execute, monitor."""
    config.validate_config()

    # Verify DB connectivity (a trivial query that returns equity or the default).
    try:
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

    runner = await start_health_server()
    brain.reset_daily_spend()

    last_crypto_scan = 0.0
    last_kalshi_scan = 0.0
    last_learning_check = datetime.now(timezone.utc)
    last_midnight_reset = datetime.now(timezone.utc).date()

    try:
        while not _shutdown.is_set():
            now = time.time()
            now_dt = datetime.now(timezone.utc)

            # 1 — midnight UTC reset.
            if now_dt.date() != last_midnight_reset:
                await midnight_reset(db)
                last_midnight_reset = now_dt.date()

            # 2 — weekly learning.
            if config.LEARNING_ENABLED and (now_dt - last_learning_check) >= timedelta(
                days=config.LEARNING_INTERVAL_DAYS
            ):
                await brain.run_weekly_learning(db)
                last_learning_check = now_dt

            # 3 — crypto scan cycle.
            if config.CRYPTO_ENABLED and (now - last_crypto_scan) >= config.CRYPTO_SCAN_INTERVAL:
                await _run_market("crypto", scanner.scan_kraken, db)
                last_crypto_scan = now

            # 4 — kalshi scan cycle.
            if config.KALSHI_ENABLED and (now - last_kalshi_scan) >= config.KALSHI_SCAN_INTERVAL:
                await _run_market("kalshi", scanner.scan_kalshi, db)
                last_kalshi_scan = now

            # 5 — monitor open positions (trailing stops, SL/TP, EOD).
            await executor.monitor_positions(db)
            if config.STOCKS_ENABLED or config.OPTIONS_ENABLED:
                await executor.close_eod_positions(db)

            # Refresh health state.
            try:
                summary = await get_portfolio_summary(db)
                STATE["equity"] = await db.get_equity()
                STATE["daily_pnl"] = summary["daily_pnl"]
                STATE["open_trades"] = summary["open_count"]
                STATE["kill_switch"] = await db.get_kill_switch()
                STATE["last_cycle"] = now_dt.isoformat()
            except Exception as exc:  # noqa: BLE001
                log.error("health state refresh failed: %s", exc)

            await asyncio.sleep(config.LOOP_SLEEP_SECONDS)
    finally:
        await runner.cleanup()
        log.info("RAID OFFLINE — %s", datetime.now(timezone.utc).isoformat())


def _install_signal_handlers(loop):
    """Wire SIGTERM/SIGINT to a graceful shutdown event (best-effort on Windows)."""
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
    """Create the event loop, install signal handlers, and run RAID to completion."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()


if __name__ == "__main__":
    run()
