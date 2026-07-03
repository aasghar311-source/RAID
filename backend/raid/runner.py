"""RAID Omega engine runner — the deterministic strategy cycle wired into the worker.

Replaces the legacy LLM brain path (behind config.USE_NEW_ENGINE). REUSES the proven
plumbing: scanner for market data, gate for risk gates, db for persistence, and
executor.monitor_positions for exits (SL/TP/MAT/trail on the trades table). This module
only owns the DECISION layer: data -> features -> regime -> strategies -> typed
candidate -> risk-sized -> gated -> booked paper trade.

Booking model: a paper strategy fires only when its entry is actionable now (price
already at resistance/support, or a market entry into a ranked/swept name), so the
candidate is booked at the reference price this cycle. A trigger-based fill via the
state machine + fill simulator is a follow-up.

Cross-sectional strategies (C6 relative-strength rotation, C7 cross-sectional momentum)
consume a universe ranking computed ONCE per cycle and threaded into every per-symbol
context via extras. C10 (liquidity-sweep reversal) reads the raw 5m candles + order book
from extras. C6 positions that fall off the leaderboard are rotated OUT on a throttled
cadence. Nothing here sizes positions (the risk manager does) or trades non-spot-long.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

import config
import gate
from signals import Signal

from raid.core import features as F
from raid.core.provider import CAP_FUTURES, CAP_MARGIN, CAP_SHORT, CAP_SPOT_LONG
from raid.core.regime import classify
from raid.core.risk import PortfolioRiskManager, PortfolioState, RiskTier
from raid.core.strategy import StrategyContext, StrategyMode
from raid.core.universe import (
    compute_universe_rankings, has_opposite, parse_strategy_tag, trade_margin, within_cooldown,
)
from raid.strategies.catalog import build_default_registry
from raid.strategies.rotation import C6_REBALANCE_HOURS

log = logging.getLogger("raid.runner")

# Strategies with real logic run PAPER; the rest stay SHADOW until their data/capability
# contract is met (promotion to PAPER is otherwise evidence-gated). C6/C7/C10 activated
# with the universe-ranking + microstructure data contract.
# C1 quarantined 2026-07-03 (removed from the paper set) — 11% win rate (1/9), -$14.98 over
# the 124-trade review (false-breakout machine). REVERSIBLE: move "RAID-C1" back here and
# drop it from _QUARANTINED to re-activate.
# 2026-07-03: C3 (shorts), C8 (pairs), C9 (carry) promoted to paper after margin/futures were
# enabled on the account (1x leverage, paper). C8/C9 are data-gated stubs — registered paper
# but produce 0 candidates until their two-leg data/execution contract is built.
_PAPER_ON_CUTOVER = ("RAID-C2", "RAID-C3", "RAID-C4", "RAID-C5", "RAID-C6", "RAID-C7", "RAID-C8", "RAID-C9", "RAID-C10")
_QUARANTINED = {"RAID-C1": "11% win rate over 9 trades, pending review"}
_RISK = PortfolioRiskManager(RiskTier.INITIAL)   # Tier 1: 0.5% risk/trade at cutover
# Margin + futures verified on the Kraken account (1x leverage only, paper). Granting SHORT
# (C3/C7-short/C8), FUTURES + MARGIN (C9) so their is_eligible capability gate passes.
_CAPABILITIES = frozenset({CAP_SPOT_LONG, CAP_SHORT, CAP_MARGIN, CAP_FUTURES})

# C6 rotation-out: close a C6 position only once its symbol falls past this rank
# (hysteresis vs the top-5 entry band, so we don't churn on a one-rank wobble). Guarded
# by a minimum ranked-universe size so a data gap never mass-closes the book.
_C6_ROTATE_OUT_RANK = 8
_MIN_RANKED_FOR_ROTATION = 12

# Kraken round-trip taker fee (matches executor.compute_pnl); inlined so the runner
# stays free of the executor->brain import chain.
_TAKER_FEE_PCT = 0.0016


def build_cutover_registry():
    reg = build_default_registry()
    for sid in _PAPER_ON_CUTOVER:
        reg.set_mode(sid, StrategyMode.PAPER)
    for sid, reason in _QUARANTINED.items():
        reg.set_mode(sid, StrategyMode.QUARANTINED)   # registered but never in paper() → not booked
        log.info("%s quarantined — %s", sid, reason)
    return reg


_REGISTRY = build_cutover_registry()

# Startup notices for the strategies enabled 2026-07-03 (margin/futures verified on account).
log.info("RAID-C3 enabled — short trend breakdown (margin verified)")
log.info("C7 shorts enabled — cross-sectional momentum short sleeve active")
log.info("RAID-C8 enabled — statistical pairs (data-gated: produces candidates only when cointegration data available)")
log.info("RAID-C9 enabled — funding carry (data-gated: needs two-leg perp/spot execution; funding rates are available)")

# Peak equity high-water mark for drawdown-based leverage de-risking. Module-level: on a
# restart it re-seeds from max(STARTING_EQUITY, current) — conservative (floors drawdown at
# the loss from the starting capital; the true pre-restart peak is not persisted).
_peak_equity = 0.0


def _effective_leverage(drawdown_pct: float):
    """Leverage to use given drawdown from peak. Returns (leverage:int, None) normally, or
    (None, 'pause'|'shutdown') at deep drawdown. Applies config.LEVERAGE_DERISKING
    (6%->2x, 10%->1x, 15%->pause, 20%->shutdown). Pure + testable."""
    lev = config.LEVERAGE_MULTIPLIER
    halt = None
    for threshold, reduced in sorted(config.LEVERAGE_DERISKING.items()):
        if drawdown_pct >= threshold:
            if reduced < 0:
                halt = "shutdown"
            elif reduced == 0:
                halt = "pause"
            else:
                lev = reduced
    if halt:
        return None, halt
    return min(lev, config.MAX_LEVERAGE), None


def _rotation_pnl(direction: str, entry: float, exit_price: float, size_usd: float) -> float:
    """Realized USD pnl net of Kraken round-trip fees — mirrors executor.compute_pnl."""
    if not entry or entry <= 0:
        return 0.0
    fee_cost = size_usd * _TAKER_FEE_PCT * 2
    if direction in ("long", "yes"):
        gross = size_usd * (exit_price - entry) / entry
    else:
        gross = size_usd * (entry - exit_price) / entry
    return gross - fee_cost


def _series(candles):
    """Extract (highs, lows, closes) from Kraken candles [ts,o,h,l,c,volume]."""
    highs, lows, closes = [], [], []
    for c in candles or []:
        if len(c) >= 5:
            highs.append(float(c[2])); lows.append(float(c[3])); closes.append(float(c[4]))
    return highs, lows, closes


def _feat(candles, sid, symbol, tf):
    highs, lows, closes = _series(candles)
    if len(closes) < 2:
        return None
    return F.build_feature_snapshot(sid, symbol, tf, highs, lows, closes)


def _spread_depth(order_book):
    """Best-effort spread/depth from the scanner's order-book dict; paper defaults."""
    try:
        bids = (order_book or {}).get("bids") or []
        asks = (order_book or {}).get("asks") or []
        if bids and asks:
            bb, ba = float(bids[0][0]), float(asks[0][0])
            mid = (bb + ba) / 2
            if mid > 0 and ba > bb:
                return (ba - bb) / mid, True
    except Exception:  # noqa: BLE001
        pass
    return 0.0004, True


def _context(sr, equity: float, ts: str, shared: dict | None = None):
    _, _, closes5 = _series(sr.ohlcv)
    if len(closes5) < 30:
        return None
    feats = {}
    for tf, candles in (("5m", sr.ohlcv), ("15m", sr.ohlcv_15m), ("30m", sr.ohlcv_30m), ("1h", sr.ohlcv_1h)):
        fs = _feat(candles, f"{sr.symbol}-{tf}-{ts}", sr.symbol, tf)
        if fs:
            feats[tf] = fs
    if "5m" not in feats:
        return None
    spread, depth_ok = _spread_depth(sr.order_book)
    px = Decimal(str(sr.current_price or feats["5m"].last_price))
    if px <= 0:
        return None
    # Base per-symbol extras + the cross-cycle shared context (rankings, open symbols,
    # rebalance gate) + the raw microstructure C10 needs.
    extras = {"equity": float(equity), "risk_pct": 0.005, "expiry_ts": ts}
    if shared:
        extras.update(shared)
    extras["candles_5m"] = sr.ohlcv
    extras["candles_15m"] = sr.ohlcv_15m
    extras["order_book"] = sr.order_book
    extras["funding_rate"] = getattr(sr, "funding_rate", 0.0)   # wired for C9 (funding carry)
    return StrategyContext(
        symbol=sr.symbol, instrument_id=sr.symbol, timestamp=ts,
        market_regime=classify(feats["5m"]).regime, features=feats,
        market_data_snapshot_id=f"{sr.symbol}-{ts}", reference_price=px,
        spread_pct=spread, depth_ok=depth_ok, capabilities=_CAPABILITIES,
        extras=extras,
    )


async def _hold_lease(db) -> bool:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    from datetime import timedelta
    # TTL = 3x the cycle cadence so the lease survives up to 2 missed cycles before another
    # worker can claim it (renewed at the start of every cycle). 5-min cycles -> 15-min TTL.
    ttl_seconds = 3 * config.BRAIN_CYCLE_MINUTES * 60
    new_expiry = (now + timedelta(seconds=ttl_seconds)).isoformat()
    return await db.try_claim_lease(1, config.WORKER_ID, now_iso, new_expiry)


async def _rotate_c6_out(open_trades, rankings, scan_results, db, ts) -> set:
    """Close RAID-C6 positions whose symbol has dropped off the leaderboard. Returns the
    set of rotated trade ids. Guarded: only runs on a real ranked universe (never on a
    data gap) and only closes a symbol that IS ranked but has clearly fallen out."""
    rotated: set = set()
    if len(rankings) < _MIN_RANKED_FOR_ROTATION:
        return rotated
    price_by_symbol = {sr.symbol: (sr.current_price or 0.0) for sr in scan_results}
    keep = {s for s, r in rankings.items() if r["rank"] <= _C6_ROTATE_OUT_RANK}
    for t in open_trades:
        if parse_strategy_tag(t.get("claude_reasoning")) != "RAID-C6":
            continue
        sym = t.get("symbol")
        r = rankings.get(sym)
        if r is None or sym in keep:            # unranked (data gap) or still strong -> hold
            continue
        price = price_by_symbol.get(sym)
        if not price or price <= 0:
            continue
        try:
            pnl = _rotation_pnl(t.get("direction"), t.get("entry_price") or 0, price, t.get("size_usd") or 0)
            await db.close_trade(t["id"], price, pnl, "c6_rotation")
            rotated.add(t["id"])
            log.info("RAID ENGINE: C6 rotate-out %s (rank=%s) pnl=$%.2f", sym, r["rank"], pnl)
        except Exception as exc:  # noqa: BLE001
            log.error("RAID ENGINE: C6 rotate-out failed for %s: %s", sym, exc)
    return rotated


async def run_strategy_cycle(scan_results, db, controls: dict) -> int:
    """One deterministic cycle. Returns the number of paper trades booked."""
    log.info("RAID ENGINE: strategy cycle start — %d symbols", len(scan_results))

    if not await _hold_lease(db):
        log.warning("RAID ENGINE: another worker holds the lease — running passive (no bookings)")
        return 0

    # Regime-log rotation: trim rows older than 48h at cycle START (before this cycle's
    # writes) so the table stays bounded at 5-min cadence (~7k rows/day otherwise).
    try:
        _deleted = await db.cleanup_regime_log(48)
        if _deleted:
            log.info("REGIME CLEANUP: deleted %d rows older than 48h", _deleted)
    except Exception as exc:  # noqa: BLE001
        log.error("RAID ENGINE: regime cleanup failed: %s", exc)

    equity = float(await db.get_equity() or config.STARTING_EQUITY)
    open_trades = await db.get_open_trades()
    max_open = int(controls.get("max_open_trades") or config.MAX_OPEN_TRADES)
    peak = max(config.STARTING_EQUITY, equity)
    ts = datetime.now(timezone.utc).isoformat()
    paper_strats = _REGISTRY.paper()

    # --- Leverage + drawdown de-risking (peak high-water mark) ---
    global _peak_equity
    _peak_equity = max(_peak_equity, config.STARTING_EQUITY, equity)
    drawdown = (_peak_equity - equity) / _peak_equity if _peak_equity > 0 else 0.0
    eff_lev, halt = _effective_leverage(drawdown)
    if halt == "shutdown":
        log.critical("RAID ENGINE: DRAWDOWN %.1f%% >= 20%% — HARD SHUTDOWN (setting kill switch)", drawdown * 100)
        try:
            await db.set_kill_switch(True, f"drawdown {drawdown * 100:.1f}% >= 20%", "runner_auto")
        except Exception as exc:  # noqa: BLE001
            log.error("RAID ENGINE: failed to set kill switch on shutdown: %s", exc)
        return 0
    entries_paused = (halt == "pause")
    if entries_paused:
        log.warning("RAID ENGINE: DRAWDOWN %.1f%% >= 15%% — pausing all entries this cycle", drawdown * 100)
    elif eff_lev != config.LEVERAGE_MULTIPLIER:
        log.info("RAID ENGINE: DRAWDOWN %.1f%% — leverage reduced to %dx", drawdown * 100, eff_lev)

    # --- Cross-sectional universe ranking (computed ONCE per cycle) ---
    rankings = compute_universe_rankings(scan_results)

    # Per-strategy last-entry time (open + recently-closed trades) drives the C6
    # rebalance throttle; the open-symbol set prevents C6/C7 from stacking a name.
    recent_closed = await db.get_closed_trades_last_n(50)
    last_entry: dict[str, str] = {}
    last_close_by_symbol: dict[str, str] = {}   # for the post-close per-symbol cooldown
    for t in list(open_trades) + list(recent_closed):
        tag = parse_strategy_tag(t.get("claude_reasoning"))
        ot = t.get("open_time")
        if tag and ot and str(ot) > last_entry.get(tag, ""):
            last_entry[tag] = str(ot)
        sym, ct = t.get("symbol"), t.get("close_time")   # open trades have close_time=None
        if sym and ct and str(ct) > last_close_by_symbol.get(sym, ""):
            last_close_by_symbol[sym] = str(ct)
    c6_rebalance_ok = not within_cooldown(last_entry.get("RAID-C6"), ts, C6_REBALANCE_HOURS)

    # --- C6 rotation-out (throttled + guarded) BEFORE new entries ---
    if c6_rebalance_ok:
        rotated = await _rotate_c6_out(open_trades, rankings, scan_results, db, ts)
        if rotated:
            open_trades = [t for t in open_trades if t.get("id") not in rotated]

    deployed_margin = sum(trade_margin(t) for t in open_trades)   # MARGIN, not notional
    open_symbols = {t.get("symbol") for t in open_trades if t.get("symbol")}
    # Per-symbol open directions for opposite-direction protection (block hedging one symbol).
    open_dirs_by_symbol: dict[str, set] = {}
    for t in open_trades:
        _s, _d = t.get("symbol"), t.get("direction")
        if _s and _d:
            open_dirs_by_symbol.setdefault(_s, set()).add(_d)
    shared = {
        "universe_rankings": rankings,
        "open_symbols": open_symbols,
        "c6_rebalance_ok": c6_rebalance_ok,
        "strategy_last_entry": last_entry,
    }

    booked = 0
    regime_tally: dict[str, int] = {}
    produced_by: dict[str, int] = {}
    shadow_tally = {"c7_shorts": 0, "c10_sweeps": 0, "c10_shadow": 0}
    for sr in scan_results:
        ctx = _context(sr, equity, ts, shared)
        if ctx is None:
            continue
        regime_tally[ctx.market_regime.value] = regime_tally.get(ctx.market_regime.value, 0) + 1

        # (a) Persist the per-symbol regime so the Regimes dashboard populates. This is
        # pure OBSERVABILITY and must run for EVERY symbol regardless of book capacity —
        # a full/at-cap book must never blank out regime classification. (Previously the
        # capacity checks below were `break`s ABOVE this line, so once the 95% deployment
        # cap was hit the loop exited on the first symbol and logged zero regimes.)
        # market=symbol (regime_log has no symbol column); detected_at is DB-defaulted.
        _f5 = ctx.feature("5m")
        await db.log_regime({
            "market": sr.symbol,
            "regime": ctx.market_regime.value,
            "reasoning": f"{ctx.market_regime.value} (raid engine)",
            "confidence": None,
            "vol_30d": (_f5.realized_vol if _f5 else None),
            "trajectory": None,
        })

        # Drawdown pause (15%+): keep classifying regimes but book NO new entries this cycle.
        if entries_paused:
            continue
        # (b) Capacity gates apply to NEW ENTRIES only. Once the book is full or the 95%
        # MARGIN deployment cap is reached, skip booking but keep classifying the rest of the
        # universe (continue, NOT break — so regime observability never goes dark).
        if len(open_trades) + booked >= max_open or booked >= config.MAX_ENTRIES_PER_CYCLE:
            continue
        if deployed_margin >= equity * config.MAX_EQUITY_DEPLOYED_PCT:
            continue

        # (c) Post-close per-symbol cooldown: at 5-min cadence, don't immediately re-enter a
        # symbol we just closed on (prevents churning the same stale setup). Wall-clock based.
        _last_close = last_close_by_symbol.get(sr.symbol)
        if _last_close and within_cooldown(_last_close, ts, config.SYMBOL_COOLDOWN_MINUTES / 60.0):
            log.info("COOLDOWN: skip %s — trade closed within %dm", sr.symbol, config.SYMBOL_COOLDOWN_MINUTES)
            continue

        # Collect candidates from every eligible paper strategy for this symbol.
        symbol_cands = []
        for strat in paper_strats:
            if not strat.is_eligible(ctx):
                continue
            cands = strat.generate_candidates(ctx)
            for c in cands:
                symbol_cands.append((strat, c))
            if cands:
                produced_by[strat.strategy_id] = produced_by.get(strat.strategy_id, 0) + len(cands)

        # Roll up shadow observations (C7 short flags, C10 detected sweeps) for logging.
        shadow_tally["c7_shorts"] += len(ctx.extras.get("_c7_shadow_shorts", []))
        shadow_tally["c10_sweeps"] += len(ctx.extras.get("_c10_sweeps", []))
        shadow_tally["c10_shadow"] += len(ctx.extras.get("_c10_shadow", []))

        if not symbol_cands:
            continue

        # (b) Per-symbol per-cycle dedupe: keep the single highest net_rr candidate
        # so two strategies agreeing on one symbol never double-book it.
        if len(symbol_cands) > 1:
            symbol_cands.sort(key=lambda sc: float(sc[1].net_rr), reverse=True)
            _dropped = [f"{s.strategy_id}(rr={c.net_rr})" for s, c in symbol_cands[1:]]
            log.info("RAID ENGINE: dedupe %s — keep %s(rr=%s), drop %s",
                     sr.symbol, symbol_cands[0][0].strategy_id, symbol_cands[0][1].net_rr, _dropped)
        strat, c = symbol_cands[0]

        # Opposite-direction protection: never open a short into an open long (or vice versa)
        # on the same symbol. Same-direction stacking is allowed.
        if has_opposite(open_dirs_by_symbol.get(sr.symbol, set()), c.direction.value):
            log.info("RAID ENGINE: skip %s %s — opposite-direction position already open",
                     sr.symbol, c.direction.value)
            continue

        # Booking-geometry guard: the runner books at reference_price, but STOP/LIMIT-entry
        # strategies anchor stop/target to a trigger/limit that can differ from px. Reject a
        # candidate whose stop/target land on the wrong side of the ACTUAL book price (which
        # would instant-stop or instant-TP) — matters most for shorts (stop can end below px).
        _epx, _sl, _tp = float(c.reference_price), float(c.stop_price), float(c.targets[0])
        _valid = (_sl < _epx < _tp) if c.direction.value == "long" else (_tp < _epx < _sl)
        if not _valid:
            log.info("RAID ENGINE: skip %s %s — degenerate geometry at book price (px=%.6f sl=%.6f tp=%.6f)",
                     sr.symbol, c.direction.value, _epx, _sl, _tp)
            continue

        # Risk-size, gate, and book the single winning candidate.
        state = PortfolioState(Decimal(str(equity)), Decimal(str(peak)))
        decision = _RISK.assess(state, c.reference_price, c.stop_price)
        if not decision.approved:
            log.info("RAID ENGINE: risk reject %s %s — %s", sr.symbol, strat.strategy_id, decision.reason)
            continue
        # Base = risk-sized notional capped at 5% equity (~$200 margin). Leverage scales the
        # position notional; margin (= base) is what counts against the 95% deployment cap.
        base_notional = min(float(decision.quantity) * float(c.reference_price), config.MAX_TRADE_SIZE_PCT * equity)
        if base_notional < 10:
            continue
        notional = base_notional * eff_lev
        margin = base_notional
        if deployed_margin + margin > equity * config.MAX_EQUITY_DEPLOYED_PCT:
            continue
        log.info("RAID ENGINE: SIZING %s $%.2f notional (%dx leverage, $%.2f margin)",
                 sr.symbol, notional, eff_lev, margin)

        sig = Signal(
            market="crypto", symbol=sr.symbol, direction=c.direction.value, confidence=0.0,
            technical_score=0.0, news_sentiment="neutral", news_headline="", news_boost=0.0,
            macro_blocked=False, block_reason="", scan_result=sr,
        )
        g = await gate.check_gate(sig, db)
        if not g.passed:
            log.info("RAID ENGINE: gate reject %s — %s", sr.symbol, g.reason)
            continue

        trade = {
            "bot_name": config.BOT_NAME, "market": "crypto", "symbol": sr.symbol,
            "direction": c.direction.value, "entry_price": float(c.reference_price),
            "exit_price": None, "size_usd": round(notional, 2), "confidence": None,
            "pnl": 0, "status": "open", "close_reason": None, "paper_mode": config.PAPER_MODE,
            "sl": float(c.stop_price), "tp": float(c.targets[0]),
            "instrument_type": "crypto", "market_regime": ctx.market_regime.value,
            "claude_reasoning": f"{strat.strategy_id} {c.entry_type.value} net_rr={c.net_rr} lev={eff_lev}x margin={margin:.2f} :: {strat.explain_decision(c, ctx)}"[:1000],
            "predicted_prob": None, "kelly_fraction": None,
        }
        tid = await db.log_trade(trade)
        if tid:
            booked += 1
            deployed_margin += margin
            open_symbols.add(sr.symbol)
            log.info("RAID ENGINE: booked %s %s %s $%.2f sl=%.6f tp=%.6f net_rr=%s",
                     strat.strategy_id, sr.symbol, c.direction.value, notional,
                     float(c.stop_price), float(c.targets[0]), c.net_rr)

    log.info(
        "RAID ENGINE: cycle complete — booked %d (regimes: %s | produced: %s | "
        "shadow: c7_shorts=%d c10_sweeps=%d c10_shadow=%d)",
        booked, regime_tally or "none", produced_by or "none",
        shadow_tally["c7_shorts"], shadow_tally["c10_sweeps"], shadow_tally["c10_shadow"],
    )
    return booked
