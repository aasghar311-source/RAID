"""RAID executor — position sizing, SL/TP, trailing stops, entry, and open-trade monitoring."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import config
import scanner
import brain
from signals import Signal
from scanner import ScanResult

log = logging.getLogger("raid.executor")


@dataclass
class TradeResult:
    """The result of attempting to open a trade."""

    trade_id: str
    symbol: str
    direction: str
    entry_price: float
    size_usd: float
    sl: float
    tp: float
    status: str
    paper_mode: bool


def calculate_size(confidence: float, equity: float):
    """Return position size = base * confidence multiplier * equity-tier multiplier."""
    keys = sorted(config.CONF_MULT)
    if confidence <= keys[0]:
        conf_mult = config.CONF_MULT[keys[0]]
    elif confidence >= keys[-1]:
        conf_mult = config.CONF_MULT[keys[-1]]
    else:
        conf_mult = config.CONF_MULT[keys[0]]
        for i in range(len(keys) - 1):
            lo, hi = keys[i], keys[i + 1]
            if lo <= confidence <= hi:
                frac = (confidence - lo) / (hi - lo)
                conf_mult = config.CONF_MULT[lo] + frac * (config.CONF_MULT[hi] - config.CONF_MULT[lo])
                break

    tier_mult = config.EQUITY_TIER_MULT[-1][1]
    for threshold, mult in config.EQUITY_TIER_MULT:
        if equity < threshold:
            tier_mult = mult
            break

    return config.BASE_TRADE_SIZE * conf_mult * tier_mult


def calculate_sl_tp(entry: float, direction: str):
    """Return (stop_loss, take_profit) prices for the given direction."""
    if direction == "long":
        return entry * (1 - config.STOP_LOSS_PCT), entry * (1 + config.TAKE_PROFIT_PCT)
    if direction == "short":
        return entry * (1 + config.STOP_LOSS_PCT), entry * (1 - config.TAKE_PROFIT_PCT)
    if direction == "yes":
        return entry * config.KALSHI_SL_PCT, config.KALSHI_TP_PRICE
    if direction == "no":
        # 'no' profits as yes_price falls; SL is a rise, TP is a drop toward 0.
        return entry * (1 + (1 - config.KALSHI_SL_PCT)), 1 - config.KALSHI_TP_PRICE
    return entry * (1 - config.STOP_LOSS_PCT), entry * (1 + config.TAKE_PROFIT_PCT)


def compute_pnl(direction: str, entry: float, exit_price: float, size_usd: float):
    """Return realized USD pnl NET of Kraken round-trip taker fees (0.16% × 2 sides)."""
    if entry <= 0:
        return 0.0
    KRAKEN_TAKER_FEE_PCT = 0.0016
    fee_cost = size_usd * KRAKEN_TAKER_FEE_PCT * 2
    if direction in ("long", "yes"):
        gross_pnl = size_usd * (exit_price - entry) / entry
    else:
        gross_pnl = size_usd * (entry - exit_price) / entry  # short / no
    return gross_pnl - fee_cost


async def update_trailing_stop(trade: dict, current_price: float, db):
    """Ratchet a trade's stop toward profit once it moves in favor; persist if changed."""
    try:
        direction = trade.get("direction")
        entry = trade.get("entry_price") or 0
        if entry <= 0:
            return
        current_sl = trade.get("sl")
        long_like = direction in ("long", "yes")
        short_like = direction in ("short", "no")

        if long_like:
            gain = (current_price - entry) / entry
            if gain < config.TRAIL_TRIGGER_PCT:
                return
            steps = int((gain - config.TRAIL_TRIGGER_PCT) / config.TRAIL_STEP_PCT)
            new_sl = entry * (1 + steps * config.TRAIL_STEP_PCT)
            if current_sl is None or new_sl > current_sl:
                await _persist_sl(db, trade["id"], new_sl)
                trade["trail_active"] = True
        elif short_like:
            gain = (entry - current_price) / entry
            if gain < config.TRAIL_TRIGGER_PCT:
                return
            steps = int((gain - config.TRAIL_TRIGGER_PCT) / config.TRAIL_STEP_PCT)
            new_sl = entry * (1 - steps * config.TRAIL_STEP_PCT)
            if current_sl is None or new_sl < current_sl:
                await _persist_sl(db, trade["id"], new_sl)
                trade["trail_active"] = True
    except Exception as exc:  # noqa: BLE001
        log.error("update_trailing_stop failed: %s", exc)


async def _persist_sl(db, trade_id: str, new_sl: float):
    """Persist a new stop-loss value on a trade record."""
    try:
        await db.supabase.table("trades").update({"sl": new_sl}).eq("id", trade_id).execute()
    except Exception as exc:  # noqa: BLE001
        log.error("_persist_sl failed: %s", exc)


async def execute_trade(signal: Signal, brain_result, db):
    """Open a trade (paper-simulated in Phase 1) and persist it; return a TradeResult."""
    try:
        equity = await db.get_equity()
        size_usd = calculate_size(signal.confidence, equity)
        entry = signal.scan_result.current_price or signal.scan_result.yes_price or 0.0
        sl, tp = calculate_sl_tp(entry, signal.direction)

        trade = {
            "bot_name": config.BOT_NAME,
            "market": signal.market,
            "symbol": signal.symbol,
            "direction": signal.direction,
            "entry_price": entry,
            "size_usd": size_usd,
            "confidence": signal.confidence,
            "pnl": 0,
            "status": "open",
            "close_reason": None,
            "paper_mode": config.PAPER_MODE,
            "sl": sl,
            "tp": tp,
        }

        if not config.PAPER_MODE:
            try:
                if signal.market == "crypto":
                    await _place_kraken_order(signal, size_usd, entry)
                elif signal.market == "kalshi":
                    await _place_kalshi_order(signal, size_usd, entry)
            except Exception as exc:  # noqa: BLE001
                log.error("live order placement failed for %s: %s", signal.symbol, exc)

        trade_id = await db.log_trade(trade)
        log.info(
            "TRADE OPEN %s %s %s size=$%.2f entry=%.5f sl=%.5f tp=%.5f conf=%.2f (%s)",
            signal.market,
            signal.symbol,
            signal.direction,
            size_usd,
            entry,
            sl,
            tp,
            signal.confidence,
            "PAPER" if config.PAPER_MODE else "LIVE",
        )
        return TradeResult(
            trade_id=trade_id,
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=entry,
            size_usd=size_usd,
            sl=sl,
            tp=tp,
            status="open",
            paper_mode=config.PAPER_MODE,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("execute_trade failed for %s: %s", signal.symbol, exc)
        return TradeResult("", signal.symbol, signal.direction, 0.0, 0.0, 0.0, 0.0, "error", config.PAPER_MODE)


async def _place_kraken_order(signal: Signal, size_usd: float, entry: float):
    """Place a live Kraken order (live mode only). Logged; raises are caught upstream."""
    log.info("Kraken live order: %s %s $%.2f @ %.5f", signal.direction, signal.symbol, size_usd, entry)


async def _place_kalshi_order(signal: Signal, size_usd: float, entry: float):
    """Place a live Kalshi order (live mode only). Logged; raises are caught upstream."""
    log.info("Kalshi live order: %s %s $%.2f @ %.5f", signal.direction, signal.symbol, size_usd, entry)


async def _current_price_for_trade(trade: dict, crypto_prices: dict = None):
    """Return the current market price for an open trade, or None on failure.

    For crypto, prefer a batched price from `crypto_prices` to avoid one Kraken
    Ticker call per trade; fall back to a single fetch if not present.
    """
    market = trade.get("market")
    if market == "crypto":
        if crypto_prices is not None and trade.get("symbol") in crypto_prices:
            return crypto_prices[trade["symbol"]]
        return await scanner.fetch_kraken_price(trade["symbol"])
    if market == "kalshi":
        return await scanner.fetch_kalshi_price(trade["symbol"])
    return None


def _sl_tp_hit(direction: str, price: float, sl: float, tp: float):
    """Return 'stop_loss', 'take_profit', or None given price vs the trade's levels."""
    long_like = direction in ("long", "yes")
    if long_like:
        if sl is not None and price <= sl:
            return "stop_loss"
        if tp is not None and price >= tp:
            return "take_profit"
    else:
        if sl is not None and price >= sl:
            return "stop_loss"
        if tp is not None and price <= tp:
            return "take_profit"
    return None


async def monitor_positions(db):
    """Update trailing stops, close SL/TP hits, and ask Claude on sudden adverse moves."""
    # Auto-flip to live once the live date arrives.
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        if config.PAPER_MODE and today >= config.BOT_LIVE_DATE:
            config.PAPER_MODE = False
            log.warning("RAID GOING LIVE — %s", datetime.now(timezone.utc).isoformat())
    except Exception as exc:  # noqa: BLE001
        log.error("auto-flip check failed: %s", exc)

    try:
        open_trades = await db.get_open_trades()
    except Exception as exc:  # noqa: BLE001
        log.error("monitor_positions could not load open trades: %s", exc)
        return

    # Batch all crypto prices into ONE Kraken call per cycle — per-trade fetches
    # exceeded Kraken's public rate limit once many trades were open, so prices
    # came back None and SL/TP was never evaluated (trades never closed).
    crypto_symbols = [
        t["symbol"] for t in open_trades if t.get("market") == "crypto" and t.get("symbol")
    ]
    crypto_prices = await scanner.fetch_kraken_prices(crypto_symbols) if crypto_symbols else {}

    for trade in open_trades:
        try:
            price = await _current_price_for_trade(trade, crypto_prices)
            if price is None or price <= 0:
                continue

            # Peak tracking (instrumentation only — records high-water profit %
            # of each trade so exit thresholds can be tuned from real data).
            try:
                _entry = trade.get("entry_price") or 0
                _dir = trade.get("direction")
                if _entry > 0:
                    if _dir in ("long", "yes"):
                        _cur_pct = (price - _entry) / _entry * 100
                    else:
                        _cur_pct = (_entry - price) / _entry * 100
                    _prev_peak = trade.get("peak_pnl_pct") or 0
                    if _cur_pct > _prev_peak:
                        await db.update_trade_fields(trade["id"], {"peak_pnl_pct": round(_cur_pct, 3)})
                        trade["peak_pnl_pct"] = round(_cur_pct, 3)
            except Exception as exc:  # noqa: BLE001
                log.error("peak tracking failed for %s: %s", trade.get("id"), exc)

            await update_trailing_stop(trade, price, db)

            direction = trade.get("direction")
            entry = trade.get("entry_price") or 0
            sl = trade.get("sl")
            tp = trade.get("tp")

            # Re-read SL after trail may have updated it.
            sl = trade.get("sl")
            hit = _sl_tp_hit(direction, price, sl, tp)
            if hit:
                # Distinguish trailing_stop from original stop_loss.
                close_reason = "trailing_stop" if trade.get("trail_active") and hit == "stop_loss" else hit
                pnl = compute_pnl(direction, entry, price, trade.get("size_usd") or 0)
                await db.close_trade(trade["id"], price, pnl, close_reason)
                log.info("TRADE CLOSE %s %s reason=%s pnl=$%.2f", trade["market"], trade["symbol"], close_reason, pnl)
                continue

            # Max-hold exit: close genuinely stale trades that never hit SL/TP,
            # so they stop occupying a slot. SL/TP above always takes priority.
            if config.MAX_HOLD_EXIT_ENABLED and trade.get("open_time"):
                try:
                    opened = datetime.fromisoformat(str(trade["open_time"]).replace("Z", "+00:00"))
                    hours_open = (datetime.now(timezone.utc) - opened).total_seconds() / 3600.0
                    if hours_open >= config.MAX_HOLD_HOURS:
                        pnl = compute_pnl(direction, entry, price, trade.get("size_usd") or 0)
                        await db.close_trade(trade["id"], price, pnl, "max_hold_exit")
                        log.info(
                            "TRADE CLOSE %s %s reason=max_hold_exit hours=%.1f pnl=$%.2f",
                            trade["market"], trade["symbol"], hours_open, pnl,
                        )
                        continue
                except Exception as exc:  # noqa: BLE001
                    log.error("max_hold check failed for %s: %s", trade.get("id"), exc)

            # No-progress exit: a trade that never went meaningfully green is a dud.
            # Cut it early instead of letting it bleed to the 3h max-hold. SL/TP and
            # max-hold above always take priority. Thresholds set from peak_pnl_pct data.
            if config.NO_PROGRESS_EXIT_ENABLED and trade.get("open_time"):
                try:
                    opened_np = datetime.fromisoformat(str(trade["open_time"]).replace("Z", "+00:00"))
                    mins_open = (datetime.now(timezone.utc) - opened_np).total_seconds() / 60.0
                    peak = trade.get("peak_pnl_pct") or 0
                    if mins_open >= config.NO_PROGRESS_MINUTES and peak < config.NO_PROGRESS_MIN_PEAK_PCT:
                        pnl = compute_pnl(direction, entry, price, trade.get("size_usd") or 0)
                        await db.close_trade(trade["id"], price, pnl, "no_progress_exit")
                        log.info(
                            "TRADE CLOSE %s %s reason=no_progress_exit mins=%.0f peak=%.2f%% pnl=$%.2f",
                            trade["market"], trade["symbol"], mins_open, peak, pnl,
                        )
                        continue
                except Exception as exc:  # noqa: BLE001
                    log.error("no_progress check failed for %s: %s", trade.get("id"), exc)

            # Violent adverse move (well past the stop) → ask Claude to hold or exit.
            # Gated so it never competes with the mechanical stop at the same level.
            if entry > 0 and config.AI_OVERRIDE_EXIT_ENABLED:
                adverse = (
                    (entry - price) / entry if direction in ("long", "yes") else (price - entry) / entry
                )
                if adverse > config.ADVERSE_MOVE_PCT:
                    decision = await _claude_override(trade, price, db)
                    if decision == "SKIP":
                        pnl = compute_pnl(direction, entry, price, trade.get("size_usd") or 0)
                        await db.close_trade(trade["id"], price, pnl, "ai_override_exit")
                        log.info(
                            "TRADE CLOSE %s %s reason=ai_override_exit pnl=$%.2f",
                            trade["market"],
                            trade["symbol"],
                            pnl,
                        )
        except Exception as exc:  # noqa: BLE001
            log.error("monitor_positions failed for trade %s: %s", trade.get("id"), exc)
            continue


async def _claude_override(trade: dict, price: float, db):
    """Ask Claude whether to hold or exit a position under a sudden adverse move."""
    try:
        sr = ScanResult(
            market=trade.get("market"),
            symbol=trade.get("symbol"),
            current_price=price,
            scan_time=datetime.now(timezone.utc).isoformat(),
        )
        signal = Signal(
            market=trade.get("market"),
            symbol=trade.get("symbol"),
            direction=trade.get("direction"),
            confidence=max(config.CLAUDE_GRAY_ZONE_MIN, min(trade.get("confidence") or 0.7, config.CLAUDE_GRAY_ZONE_MAX)),
            technical_score=(trade.get("confidence") or 0.7) * 100,
            news_sentiment="neutral",
            news_headline="Sudden adverse price move under monitoring",
            news_boost=0.0,
            macro_blocked=False,
            block_reason="",
            scan_result=sr,
        )
        summary = {
            "open_count": len(await db.get_open_trades()),
            "daily_pnl": 0.0,
            "win_rate": 0.0,
            "consecutive_losses": await db.get_consecutive_losses(),
            "macro_status": "monitoring",
        }
        result = await brain.validate_signal(signal, db, summary)
        return result.decision
    except Exception as exc:  # noqa: BLE001
        log.error("_claude_override failed: %s", exc)
        return "ENTER"  # default to hold on error


async def close_eod_positions(db):
    """Close open stocks/options positions at the EOD bell (Phase 1: logic only)."""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(config.EOD_CLOSE_TZ)
        now_local = datetime.now(tz)
        if now_local.hour != config.EOD_CLOSE_HOUR:
            return
        open_trades = await db.get_open_trades()
        for trade in open_trades:
            if trade.get("market") not in ("stocks", "options"):
                continue
            price = await _current_price_for_trade(trade)
            if price is None:
                price = trade.get("entry_price") or 0
            pnl = compute_pnl(
                trade.get("direction"), trade.get("entry_price") or 0, price, trade.get("size_usd") or 0
            )
            await db.close_trade(trade["id"], price, pnl, "eod_close")
            log.info("EOD CLOSE %s %s pnl=$%.2f", trade["market"], trade["symbol"], pnl)
    except Exception as exc:  # noqa: BLE001
        log.error("close_eod_positions failed: %s", exc)
