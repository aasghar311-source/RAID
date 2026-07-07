"""RAID executor — position sizing, SL/TP, trailing stops, entry, and open-trade monitoring."""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import config
import costs
import scanner
import brain
from signals import Signal
from scanner import ScanResult
from raid.execution.time_stops import c10_time_stop_due, classify_stop_reason, no_progress_exit_due
from raid.execution.instrumentation import excursion_update, minutes_since

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
    """Return realized USD pnl NET of this account's real all-in round-trip cost — taker
    0.40%/side x2 + margin-open + spread + slippage (~1.04% of notional), from the single
    source costs.realized_round_trip_cost_pct(). Charged on NOTIONAL (size_usd), both legs."""
    if entry <= 0:
        return 0.0
    fee_cost = size_usd * costs.realized_round_trip_cost_pct()
    if direction in ("long", "yes"):
        gross_pnl = size_usd * (exit_price - entry) / entry
    else:
        gross_pnl = size_usd * (entry - exit_price) / entry  # short / no
    return gross_pnl - fee_cost


def price_too_stale(age_seconds) -> bool:
    """True if a price is too old to act on at an exit decision (fail-closed guard).
    age_seconds is how long ago the price was fetched; None means unknown -> treat fresh."""
    return age_seconds is not None and age_seconds > config.STALE_PRICE_SECONDS


def fill_slippage_pct(direction: str, stop, fill, entry) -> float:
    """Signed % of entry by which an exit filled BEYOND its stop. Negative = the fill was
    WORSE than the stop (price gapped through it between samples); ~0 = filled at the stop.
    Diagnostic only — quantifies trailing-stop slippage on real closes."""
    if not entry or entry <= 0 or stop is None or fill is None:
        return 0.0
    if direction in ("long", "yes"):
        return (fill - stop) / entry * 100.0   # fill below stop -> negative
    return (stop - fill) / entry * 100.0        # short: fill above stop -> negative


def _trail_fee_floor(entry: float, long_like: bool) -> float:
    """Break-even floor for the trailed stop: a locked exit at this price nets ~0 after the
    REAL round-trip cost. Sourced from costs.realized_round_trip_cost_pct() (the single source
    corrected in 1af3446, ~1.04%) so it can never drift from the fee model again. Long: entry
    above by the cost; short: entry below by the cost. NOTE: with the current trail trigger
    (1.5%) and lock (0.85) the locked stop is >= entry*1.01275, so this floor is non-binding
    today — it only clamps if a future lower trigger would otherwise lock a net-loss level."""
    rt = costs.realized_round_trip_cost_pct()
    return entry * (1 + rt) if long_like else entry * (1 - rt)


def exit_price_from_quote(direction, quote, max_spread_pct: float):
    """Return (exit_price, side) for an OPEN position from a live Kraken quote.

    LONG positions exit at the BID (you sell into the bid); SHORT positions exit at the ASK
    (you buy back at the ask) — the price a paper fill would actually get, and one that keeps
    moving as makers requote even when last-trade is frozen between prints. Fails CLOSED to
    last-trade (with a reason) when the book is invalid (crossed/zero/one-sided) or the spread
    is wider than max_spread_pct. Pure + testable."""
    q = quote or {}
    last = float(q.get("last") or 0.0)
    bid = float(q.get("bid") or 0.0)
    ask = float(q.get("ask") or 0.0)
    long_like = direction in ("long", "yes")
    if bid > 0 and ask > 0 and bid < ask:
        mid = (bid + ask) / 2.0
        if mid > 0 and (ask - bid) / mid <= max_spread_pct:
            return (bid, "bid") if long_like else (ask, "ask")
        return (last if last > 0 else mid), "last(wide_spread)"
    return (last, "last(invalid_book)")


async def _exit_price(trade: dict, crypto_quotes: dict = None):
    """Exit-decision price for an open trade as (price, quote_or_None, side_label). Crypto with
    a valid book uses the live quote side (bid long / ask short); otherwise fails closed to a
    single-fetch last-trade (crypto) or the Kalshi price. Only the EXIT path uses this — entries
    keep their own price source."""
    market = trade.get("market")
    sym = trade.get("symbol")
    if market == "crypto" and crypto_quotes and sym in crypto_quotes:
        q = crypto_quotes[sym]
        price, side = exit_price_from_quote(trade.get("direction"), q, config.MAX_EXIT_SPREAD_PCT)
        return price, q, side
    price = await _current_price_for_trade(trade, None)   # fallback: last-trade / kalshi
    return price, None, "last(fallback)"


def _quote_log_detail(quote, side) -> str:
    """Compact bid/ask/last/side/spread suffix for the trail log — lets a live trace SHOW the
    quote moving while last-trade is frozen. Empty when no quote (fallback / unit tests)."""
    if not quote:
        return f" [{side}]" if side else ""
    b = float(quote.get("bid") or 0.0); a = float(quote.get("ask") or 0.0); l = float(quote.get("last") or 0.0)
    sp = ((a - b) / ((a + b) / 2.0) * 100.0) if (a > 0 and b > 0) else 0.0
    return f" [{side} bid={b:.6f} ask={a:.6f} last={l:.6f} spread={sp:.3f}%]"


async def update_trailing_stop(trade: dict, current_price: float, db, quote=None, side=None):
    """Ratchet a trade's stop toward profit once it moves in favor; persist if changed.
    Late trail: single 85% lock once +1.5% (config.TRAIL_TRIGGER_PCT) is reached.
    Insurance only — TP at 2.5% remains the primary exit. quote/side are for the evidence log
    only; current_price is already the correct exit side (bid long / ask short)."""
    try:
        direction = trade.get("direction")
        entry = trade.get("entry_price") or 0
        if entry <= 0:
            return
        current_sl = trade.get("sl")
        symbol = trade.get("symbol", "?")
        long_like = direction in ("long", "yes")
        short_like = direction in ("short", "no")

        if long_like:
            gain = (current_price - entry) / entry
            if gain < config.TRAIL_TRIGGER_PCT:
                return
            lock_pct = 0.85
            new_sl = entry * (1 + gain * lock_pct)
            # Fee-protected floor (real round-trip cost, SSOT): never trail to a level that
            # nets a loss after real fees.
            fee_floor = _trail_fee_floor(entry, True)
            new_sl = max(new_sl, fee_floor)
            if current_sl is None or new_sl > current_sl:
                await _persist_sl(db, trade["id"], new_sl)
                trade["trail_active"] = True
                log.info("TRAIL: %s lock 85%% peak_gain=+%.2f%% price=%.6f -> trail_stop %.6f%s",
                         symbol, gain * 100, current_price, new_sl, _quote_log_detail(quote, side))
        elif short_like:
            gain = (entry - current_price) / entry
            if gain < config.TRAIL_TRIGGER_PCT:
                return
            lock_pct = 0.85
            new_sl = entry * (1 - gain * lock_pct)
            # Fee-protected floor (real round-trip cost, SSOT): never trail to a level that
            # nets a loss after real fees.
            fee_floor = _trail_fee_floor(entry, False)
            new_sl = min(new_sl, fee_floor)
            if current_sl is None or new_sl < current_sl:
                await _persist_sl(db, trade["id"], new_sl)
                trade["trail_active"] = True
                log.info("TRAIL: %s lock 85%% peak_gain=+%.2f%% price=%.6f -> trail_stop %.6f%s",
                         symbol, gain * 100, current_price, new_sl, _quote_log_detail(quote, side))
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


def _assert_live_orders_allowed(venue: str) -> None:
    """Fail-closed execution boundary. Refuse any live order unless config.live_orders_allowed()
    is True; if that check itself raises, refuse too (fail-closed). They default safe, so this
    ALWAYS raises today — the live-order stubs below are unreachable for live. Adds no live path."""
    try:
        allowed = config.live_orders_allowed()
    except Exception as exc:  # noqa: BLE001 — a raising flag check must still block
        raise RuntimeError(
            "BLOCKED live %s order — flag check errored (%r); fail-closed" % (venue, exc)
        ) from exc
    if not allowed:
        raise RuntimeError(
            "BLOCKED live %s order — paper-only safety engaged "
            "(PAPER_MODE=%s PAPER_ONLY=%s LIVE_TRADING_ENABLED=%s KRAKEN_LIVE_ENABLED=%s)"
            % (venue, config.PAPER_MODE, config.PAPER_ONLY,
               config.LIVE_TRADING_ENABLED, config.KRAKEN_LIVE_ENABLED)
        )


async def _place_kraken_order(signal: Signal, size_usd: float, entry: float):
    """Place a live Kraken order (live mode only). Logged; raises are caught upstream."""
    _assert_live_orders_allowed("kraken")
    log.info("Kraken live order: %s %s $%.2f @ %.5f", signal.direction, signal.symbol, size_usd, entry)


async def _place_kalshi_order(signal: Signal, size_usd: float, entry: float):
    """Place a live Kalshi order (live mode only). Logged; raises are caught upstream."""
    _assert_live_orders_allowed("kalshi")
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


# ── B5: quote-path flight recorder ────────────────────────────────────────────
# Bounded in-memory buffer flushed batched + fire-and-forget so the 1s exit loop NEVER blocks on a
# DB write. Records open-position quote evidence (bid/ask/mid/spread/effective exit/MFE/MAE/source/
# freshness/validity) into position_quote_paths so exit-engine changes can later be replayed.
_quote_path_buffer: list = []
_quote_flush_tasks: set = set()
_QUOTE_PATH_FLUSH_AT = 200       # flush when the buffer reaches this many records
_QUOTE_PATH_MAX_BUFFER = 5000    # hard cap: a persistent flush failure can't grow memory unbounded


def _buffer_quote_path(trade, price, quote, side, price_age):
    """Append one quote-path record to the in-memory buffer. SYNCHRONOUS, O(1), NO I/O — it must
    never block the 1s exit loop. Fully wrapped: a capture error can never affect an exit."""
    try:
        if len(_quote_path_buffer) >= _QUOTE_PATH_MAX_BUFFER:
            del _quote_path_buffer[0]                     # drop oldest (ring-buffer safety)
        q = quote or {}
        bid = float(q.get("bid") or 0.0) or None
        ask = float(q.get("ask") or 0.0) or None
        mid = ((bid + ask) / 2.0) if (bid and ask) else None
        spread = ((ask - bid) / mid) if (mid and mid > 0) else None
        _quote_path_buffer.append({
            "trade_id": trade.get("id"), "pair": trade.get("symbol"),
            "ts": datetime.now(timezone.utc).isoformat(),
            "bid": bid, "ask": ask, "mid": mid, "spread": spread,
            "effective_exit_price": price, "direction": trade.get("direction"),
            "mfe": trade.get("peak_pnl_pct"), "mae": trade.get("mae_pct"),
            "source": side, "freshness_s": round(float(price_age or 0.0), 3),
            "quote_validity": side in ("bid", "ask"),
        })
    except Exception:  # noqa: BLE001 — capture must never affect the exit loop
        pass


def _spawn_flush(coro):
    """Fire-and-forget a batched quote-path flush without awaiting it (keeps the exit loop
    non-blocking). Holds a task reference so it is not GC'd mid-flight; drops it on completion."""
    t = asyncio.create_task(coro)
    _quote_flush_tasks.add(t)
    t.add_done_callback(_quote_flush_tasks.discard)
    return t


def _maybe_flush_quote_paths(db):
    """If the buffer has reached the flush threshold, hand a batch to a fire-and-forget writer and
    clear it immediately. NON-BLOCKING — returns at once; the DB write runs concurrently."""
    if not _quote_path_buffer or not hasattr(db, "persist_quote_paths"):
        return
    if len(_quote_path_buffer) < _QUOTE_PATH_FLUSH_AT:
        return
    batch = _quote_path_buffer[:]
    _quote_path_buffer.clear()
    _spawn_flush(db.persist_quote_paths(batch))


async def monitor_positions(db):
    """Update trailing stops, close SL/TP hits, and ask Claude on sudden adverse moves.
    PAPER MODE IS PERMANENT — there is no date-based auto-flip to live. Live activation,
    if ever, must be an explicit operator-approved gated change (RAID Omega rebuild rule)."""
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
    # Exit decisions read the live QUOTE (bid/ask), not last-trade — last-trade freezes for
    # minutes between prints on illiquid pairs while the book keeps requoting. Same one Ticker
    # call; bid/ask were previously discarded.
    crypto_quotes = await scanner.fetch_kraken_quotes(crypto_symbols) if crypto_symbols else {}
    # Monotonic stamp of when the batch was fetched. A trade processed late in a slow loop
    # ages relative to this; see the staleness guard below.
    _prices_fetched_at = time.monotonic()

    # B5: flush prior ticks' buffered quote-path records (fire-and-forget; never blocks this loop).
    _maybe_flush_quote_paths(db)

    for trade in open_trades:
        try:
            # Live-quote exit price: bid for a long, ask for a short (fail-closed to last-trade
            # on an invalid/wide book). _quote/_side carried for the trail evidence log.
            price, _quote, _side = await _exit_price(trade, crypto_quotes)
            if price is None or price <= 0:
                continue

            # Age of the price actually used: batch-sourced crypto prices age with the loop's
            # sequential processing time; a single-fetch fallback is fetched fresh here (~0s).
            # Fail closed — never trail or stop on a price that has gone stale (Rule: fail closed).
            _from_batch = trade.get("symbol") in crypto_quotes
            price_age = (time.monotonic() - _prices_fetched_at) if _from_batch else 0.0
            if price_too_stale(price_age):
                log.warning(
                    "STALE PRICE: %s %s age=%.1fs > %ds — skipping exit checks this tick (fail closed)",
                    trade.get("market"), trade.get("symbol"), price_age, config.STALE_PRICE_SECONDS,
                )
                continue

            # Excursion tracking (instrumentation only — high-water MFE + its timing and the
            # full adverse excursion MAE + its timing, so exit thresholds and winner-shape can
            # be tuned from real data). Reuses the live `price` already fetched (no extra API
            # call); the exit ladder below is untouched. peak_pnl_pct keeps its old semantics
            # (floored at 0), now joined by mfe/mae timing + mae_pct.
            try:
                _entry = trade.get("entry_price") or 0
                _dir = trade.get("direction")
                if _entry > 0:
                    if _dir in ("long", "yes"):
                        _cur_pct = (price - _entry) / _entry * 100
                    else:
                        _cur_pct = (_entry - price) / _entry * 100
                    _changed = excursion_update(
                        trade.get("peak_pnl_pct") or 0, trade.get("mae_pct"),
                        _cur_pct, minutes_since(trade.get("open_time")),
                    )
                    if _changed:
                        await db.update_trade_fields(trade["id"], _changed)
                        trade.update(_changed)
            except Exception as exc:  # noqa: BLE001
                log.error("excursion tracking failed for %s: %s", trade.get("id"), exc)

            # B5: record this tick's quote-path evidence (append-only; the batched DB write is
            # fire-and-forget — see _maybe_flush_quote_paths — so the exit loop is never blocked).
            if config.QUOTE_PATH_CAPTURE and hasattr(db, "persist_quote_paths"):
                _buffer_quote_path(trade, price, _quote, _side, price_age)

            await update_trailing_stop(trade, price, db, quote=_quote, side=_side)

            direction = trade.get("direction")
            entry = trade.get("entry_price") or 0
            sl = trade.get("sl")
            tp = trade.get("tp")

            # Re-read SL after trail may have updated it.
            sl = trade.get("sl")
            hit = _sl_tp_hit(direction, price, sl, tp)
            if hit:
                # TP EXTENSION: disabled — capped at 2.5%. Extending to 7.5-15% never
                # got hit (0/314 TPs reached historically); take the TP when it fills.
                if hit == "take_profit" and trade.get("trail_active"):
                    log.info("TP EXTENSION: disabled — capped at 2.5%% (%s %s)",
                             trade["market"], trade["symbol"])
                # Distinguish trailing_stop from a true stop_loss by where the SL sits
                # relative to entry (trail_active is in-memory only and never persisted, so
                # deriving from the persisted SL is the accurate signal — see time_stops).
                close_reason = classify_stop_reason(direction, entry, sl) if hit == "stop_loss" else hit
                pnl = compute_pnl(direction, entry, price, trade.get("size_usd") or 0)
                await db.close_trade(trade["id"], price, pnl, close_reason)
                log.info("TRADE CLOSE %s %s reason=%s pnl=$%.2f", trade["market"], trade["symbol"], close_reason, pnl)
                # Trailing-stop forensics: quantify how far the fill slipped past the trailed
                # stop and how old the price was. Negative slip = filled worse than the stop
                # (price gapped through it between 1s samples); large price_age = loop starvation.
                # This is the evidence artifact for the "trail locked below the peak" report.
                if close_reason == "trailing_stop":
                    log.info(
                        "TRAIL EXIT %s %s: stop=%.6f fill=%.6f(%s) slip=%.3f%% peak=%.2f%% price_age=%.1fs pnl=$%.2f",
                        trade.get("market"), trade.get("symbol"), sl, price, _side,
                        fill_slippage_pct(direction, sl, price, entry),
                        float(trade.get("peak_pnl_pct") or 0.0), price_age, pnl,
                    )
                continue

            # C10 liquidity-sweep fast time stop (90m). Sweeps resolve quickly, so a
            # RAID-C10 position gets a far tighter cap than the 3h trend max-hold. The
            # predicate is tag-scoped (see raid.execution.time_stops) so C1-C5 trades are
            # never affected; it runs before the MAT checkpoints so it is never pre-empted.
            if c10_time_stop_due(trade.get("claude_reasoning"), trade.get("open_time")):
                pnl = compute_pnl(direction, entry, price, trade.get("size_usd") or 0)
                await db.close_trade(trade["id"], price, pnl, "sweep_time_stop")
                log.info("TRADE CLOSE %s %s reason=sweep_time_stop pnl=$%.2f",
                         trade["market"], trade["symbol"], pnl)
                continue

            # No-progress exit — cut a stalled trade BEFORE the 3h MAT. Fires at 90 min if
            # the CURRENT gain is still under +0.3% (data: such trades almost never recover;
            # they drift to a MAT death at ~-$2.04). Placed ahead of MAT so a stalled trade
            # exits at 90 min instead of waiting to 180 min.
            if config.NO_PROGRESS_EXIT_ENABLED and trade.get("open_time"):
                try:
                    _np_opened = datetime.fromisoformat(str(trade["open_time"]).replace("Z", "+00:00"))
                    _np_mins = (datetime.now(timezone.utc) - _np_opened).total_seconds() / 60.0
                    if no_progress_exit_due(direction, entry, price, _np_mins,
                                            config.NO_PROGRESS_CHECK_MINUTES, config.NO_PROGRESS_MIN_GAIN_PCT):
                        _np_gain = (price - entry) / entry if direction in ("long", "yes") else (entry - price) / entry
                        pnl = compute_pnl(direction, entry, price, trade.get("size_usd") or 0)
                        await db.close_trade(trade["id"], price, pnl, "no_progress_exit")
                        log.info(
                            "NO PROGRESS: %s %s closed at %.0f min — gain %.2f%% < %.2f%% threshold pnl=$%.2f",
                            trade["market"], trade["symbol"], _np_mins, _np_gain * 100,
                            config.NO_PROGRESS_MIN_GAIN_PCT * 100, pnl,
                        )
                        continue
                except Exception as exc:  # noqa: BLE001
                    log.error("no_progress check failed for %s: %s", trade.get("id"), exc)

            # MAT (Matured Exit) system — time-based checkpoints.
            # 0-2h: normal trading (SL/trail/TP above handle it)
            # 2h+:  close if profit >= 0.5% (matured_profit)
            # 2-3h: close if profit >= 0.25% (breakeven_exit — covers fees)
            # 3h:   close regardless (max_hold_exit)
            if config.MAX_HOLD_EXIT_ENABLED and trade.get("open_time"):
                try:
                    opened = datetime.fromisoformat(str(trade["open_time"]).replace("Z", "+00:00"))
                    hours_open = (datetime.now(timezone.utc) - opened).total_seconds() / 3600.0
                    size_usd = trade.get("size_usd") or 0
                    pnl_now = compute_pnl(direction, entry, price, size_usd)
                    profit_pct = pnl_now / size_usd if size_usd > 0 else 0

                    # 3h hard ceiling — close no matter what
                    if hours_open >= config.MAX_HOLD_HOURS:
                        await db.close_trade(trade["id"], price, pnl_now, "max_hold_exit")
                        log.info(
                            "TRADE CLOSE %s %s reason=max_hold_exit hours=%.1f pnl=$%.2f",
                            trade["market"], trade["symbol"], hours_open, pnl_now,
                        )
                        continue

                    # 2h+ checkpoint — take matured profit
                    if hours_open >= config.MAT_CHECKPOINT_HOURS:
                        if profit_pct >= config.MAT_PROFIT_PCT:
                            await db.close_trade(trade["id"], price, pnl_now, "matured_profit")
                            log.info(
                                "TRADE CLOSE %s %s reason=matured_profit hours=%.1f profit=%.2f%% pnl=$%.2f",
                                trade["market"], trade["symbol"], hours_open, profit_pct * 100, pnl_now,
                            )
                            continue

                        # 2-3h window — take breakeven (covers fees)
                        if profit_pct >= config.MAT_BREAKEVEN_PCT:
                            await db.close_trade(trade["id"], price, pnl_now, "breakeven_exit")
                            log.info(
                                "TRADE CLOSE %s %s reason=breakeven_exit hours=%.1f profit=%.2f%% pnl=$%.2f",
                                trade["market"], trade["symbol"], hours_open, profit_pct * 100, pnl_now,
                            )
                            continue

                except Exception as exc:  # noqa: BLE001
                    log.error("mat_exit check failed for %s: %s", trade.get("id"), exc)

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
