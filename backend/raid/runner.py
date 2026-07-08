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

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

import config
import costs
import gate
from signals import Signal

from raid.core import features as F, liquidity, tiers
from raid.core.provider import CAP_FUTURES, CAP_MARGIN, CAP_SHORT, CAP_SPOT_LONG
from raid.core.regime import classify
from raid.core.risk import (
    TIER_LIMITS, PortfolioRiskManager, PortfolioState, RiskTier, aggregate_open_risk,
    effective_tier, graduated_size_decision, portfolio_cap_reason, symbol_cluster_index,
)
from raid.core.strategy import StrategyContext, StrategyMode
from raid.core.universe import (
    capped_leverage, compute_universe_rankings, concentration_reject_reason, has_opposite,
    is_margin_eligible, kraken_max_leverage, open_concentration_counts, parse_strategy_tag,
    trade_margin, within_cooldown,
)
from raid.strategies.catalog import build_default_registry
from raid.strategies.rotation import C6_REBALANCE_HOURS

log = logging.getLogger("raid.runner")

# Strategies with real logic run PAPER; the rest stay SHADOW until their data/capability
# contract is met (promotion to PAPER is otherwise evidence-gated). C6/C7/C10 activated
# with the universe-ranking + microstructure data contract.
# 2026-07-03: C3 (shorts), C8 (pairs), C9 (carry) promoted to paper after margin/futures were
# enabled on the account. C8/C9 are data-gated stubs — produce 0 candidates until their two-leg
# contract is built. C1 was quarantined (11% win, false-breakout machine) then UN-quarantined
# with a 1.5x breakout-volume confirmation filter (see trend.py) to cut the false breakouts.
# All ten are paper (8 produce candidates; C8/C9 are stubs). REVERSIBLE: to re-quarantine a
# strategy, drop it from _PAPER_ON_CUTOVER and add it to _QUARANTINED.
_PAPER_ON_CUTOVER = ("RAID-C1", "RAID-C2", "RAID-C3", "RAID-C4", "RAID-C5", "RAID-C6", "RAID-C7", "RAID-C8", "RAID-C9", "RAID-C10")
_QUARANTINED: dict = {}
_RISK = PortfolioRiskManager(RiskTier.INITIAL)   # Tier 1: 0.5% risk/trade at cutover
# Margin + futures verified on the Kraken account (1x leverage only, paper). Granting SHORT
# (C3/C7-short/C8), FUTURES + MARGIN (C9) so their is_eligible capability gate passes.
_MARGIN_CAPS = frozenset({CAP_SPOT_LONG, CAP_SHORT, CAP_MARGIN, CAP_FUTURES})
_SPOT_ONLY_CAPS = frozenset({CAP_SPOT_LONG})


def _capabilities_for(symbol):
    """Per-pair capability set (replaces the old blanket grant): a Kraken margin-eligible
    pair gets full spot-long/short/margin/futures; a spot-only or unknown pair gets spot-long
    only, so a short/margin strategy's is_eligible() fails on it (fail closed)."""
    return _MARGIN_CAPS if is_margin_eligible(symbol) else _SPOT_ONLY_CAPS

# C6 rotation-out: close a C6 position only once its symbol falls past this rank
# (hysteresis vs the top-5 entry band, so we don't churn on a one-rank wobble). Guarded
# by a minimum ranked-universe size so a data gap never mass-closes the book.
_C6_ROTATE_OUT_RANK = 8
_MIN_RANKED_FOR_ROTATION = 12

def build_cutover_registry():
    reg = build_default_registry()
    for sid in _PAPER_ON_CUTOVER:
        reg.set_mode(sid, StrategyMode.PAPER)
    for sid, reason in _QUARANTINED.items():
        reg.set_mode(sid, StrategyMode.QUARANTINED)   # registered but never in paper() → not booked
        log.info("%s quarantined — %s", sid, reason)
    return reg


_REGISTRY = build_cutover_registry()

# Startup notices — gated on the ACTUAL config flags + registry so the boot log tells the truth
# (not hardcoded "enabled" claims). Log text only; no behaviour change.
_paper_ids = sorted(s.strategy_id for s in _REGISTRY.paper())
log.info("RAID registry: %d paper strategies — %s", len(_paper_ids), ", ".join(_paper_ids) or "none")
log.info(
    "C7 short sleeve: %s",
    "PAPER — TREND_DOWN shorts booked (C7_SHORT_ENABLED=True)" if config.C7_SHORT_ENABLED
    else "SHADOW-ONLY — no C7 shorts booked (C7_SHORT_ENABLED=False)",
)
log.info("RAID-C3: short trend breakdown (paper; shorts require per-pair margin eligibility)")
log.info("RAID-C8/C9: registered paper but DATA-GATED stubs — produce 0 candidates")

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


def resolve_peak(persisted_peak: float, in_memory_peak: float, starting_equity: float, equity: float) -> float:
    """Drawdown high-water mark = max(persisted peak, in-memory peak, starting equity, current
    equity). Pure + testable. Seeding from the PERSISTED peak is what stops a restart from clearing
    a drawdown pause (the in-memory global re-seeds to 0 on boot). With persistence off / no
    persisted value, this reduces to the legacy max(in_memory_peak, starting, equity)."""
    return max(
        float(persisted_peak or 0.0), float(in_memory_peak or 0.0),
        float(starting_equity or 0.0), float(equity or 0.0),
    )


def _rotation_pnl(direction: str, entry: float, exit_price: float, size_usd: float) -> float:
    """Realized USD pnl net of the real all-in round-trip cost — mirrors executor.compute_pnl
    via the single source costs.realized_round_trip_cost_pct()."""
    if not entry or entry <= 0:
        return 0.0
    fee_cost = size_usd * costs.realized_round_trip_cost_pct()
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


def _real_spread_depth(order_book):
    """B3: REAL spread + total executable depth (USD) from the scanner's ACTUAL bid_walls/ask_walls
    shape. The live decision path still uses _spread_depth (which reads the wrong 'bids'/'asks' keys
    and always returns the 0.0004 fallback); this is the measure-first fix — computed and LOGGED,
    not yet fed into decisions. Returns (spread_pct, depth_usd, ok)."""
    try:
        bids = (order_book or {}).get("bid_walls") or []
        asks = (order_book or {}).get("ask_walls") or []
        if bids and asks:
            bb = float(bids[0].get("price")); ba = float(asks[0].get("price"))
            mid = (bb + ba) / 2.0
            if mid > 0 and ba > bb:
                spread = (ba - bb) / mid
                depth = (sum(float(w.get("usd") or 0.0) for w in bids)
                         + sum(float(w.get("usd") or 0.0) for w in asks))
                return spread, depth, True
    except Exception:  # noqa: BLE001
        pass
    return None, None, False


def completed_candle_would_drop(candles, now_epoch, interval_s: int = 300):
    """B2: is the LATEST 5m bar still forming (opened within the current, unfinished interval)?
    Such a bar would be dropped under completed-candle enforcement. Pure; measure-first only — the
    live path does NOT drop it yet. Returns (would_drop, last_bar_ts, age_s)."""
    try:
        if not candles:
            return False, None, None
        last_ts = int(float(candles[-1][0]))
        window_start = int(now_epoch // interval_s) * interval_s
        return (last_ts >= window_start), last_ts, int(now_epoch - last_ts)
    except Exception:  # noqa: BLE001
        return False, None, None


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
    spread, depth_ok = _spread_depth(sr.order_book)   # legacy 0.0004 fallback
    # A.1: the REAL spread/depth (B3 computed+logged it as shadow). Enforcing now, the gate PRICES on
    # it: unknown book -> spread=None (build_candidate rejects, fail-closed; no fallback pricing).
    _rs, _rd, _rok = _real_spread_depth(sr.order_book)
    if _rok:
        log.info("SPREAD_DEPTH_SHADOW symbol=%s real_spread=%.5f real_depth_usd=%.0f "
                 "fallback_spread=%.5f enforced=%s", sr.symbol, _rs, _rd, spread,
                 config.ENFORCE_REAL_SPREAD_DEPTH)
    if config.ENFORCE_REAL_SPREAD_DEPTH:
        spread = _rs if _rok else None    # price on real spread; None = fail-closed reject downstream
        depth_ok = _rok
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
        spread_pct=spread, depth_ok=depth_ok, capabilities=_capabilities_for(sr.symbol),
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


def _pf(x):
    return ("%.4f" % x) if isinstance(x, (int, float)) else "na"


async def _pair_liquidity_shadow(scan_results, db, ts, now_epoch):
    """C.6 Appendix-C §2 pair-liquidity metrics (SHADOW — measure-only). Computes the 15 metrics per
    scanned pair (completed-candle, USD-quote), logs a universe summary INCLUDING the completed-vs-
    forming volume_ratio A.2-pass shift (B.4 fold-in), and batch-persists to pair_liquidity_metrics.
    Feeds NO gate (C.8 does). Never raises into the cycle."""
    if not hasattr(db, "persist_pair_liquidity"):   # test double -> skip (no network in unit tests)
        return
    try:
        rows = []
        for sr in scan_results:
            atrp = liquidity.atr_pct_1h(getattr(sr, "ohlcv_1h", None), sr.current_price)
            m = liquidity.compute_pair_liquidity(
                sr.symbol, sr.ohlcv, sr.order_book, sr.current_price, now_epoch,
                atr_pct=atrp, volume_24h_usd=getattr(sr, "volume_24h", None),
                ohlcv_1d=getattr(sr, "ohlcv_1d", None))
            m["cycle_ts"] = ts
            rows.append(m)
        if not rows:
            return

        def _med(vals):
            xs = sorted(v for v in vals if v is not None)
            return xs[len(xs) // 2] if xs else None

        def _fmt(v):
            return ("%.4f" % v) if v is not None else "NA"

        spr_med = _med([r["spread_pct"] for r in rows])
        vrc_med = _med([r["volume_ratio"] for r in rows])
        vrf_med = _med([r["volume_ratio_forming"] for r in rows])
        pass_c = sum(1 for r in rows if (r["volume_ratio"] or 0) >= config.MIN_VOLUME_RATIO)
        pass_f = sum(1 for r in rows if (r["volume_ratio_forming"] or 0) >= config.MIN_VOLUME_RATIO)
        log.info("PAIR_LIQUIDITY_SHADOW pairs=%d spread_med=%s vr_completed_med=%s vr_forming_med=%s "
                 "A2pass completed=%d forming=%d (B.4 shift=%+d) — measure-only (migration 010)",
                 len(rows), _fmt(spr_med), _fmt(vrc_med), _fmt(vrf_med), pass_c, pass_f, pass_c - pass_f)
        written = await db.persist_pair_liquidity(rows)
        if written:
            log.info("PAIR_LIQUIDITY_SHADOW persisted rows=%d", written)

        # C.7 tier classification (SHADOW — log-only; no persistence, no gating). Each pair earns the
        # best tier its real §2 metrics support; logs the CORE/AGGRESSIVE/OPPORTUNISTIC/SHADOW/DISABLED
        # distribution so the universe spread is visible before C.8 enforces anything.
        dist = {t: [] for t in tiers.TIER_ORDER}
        for r in rows:
            tier, _ = tiers.classify_tier(r)
            dist[tier].append(r["symbol"])
        log.info("PAIR_TIER_SHADOW CORE=%d%s AGGRESSIVE=%d%s OPPORTUNISTIC=%d SHADOW=%d DISABLED=%d "
                 "(of %d) — measure-only (no gating)",
                 len(dist["CORE"]), (":" + ",".join(dist["CORE"][:8]) if dist["CORE"] else ""),
                 len(dist["AGGRESSIVE"]), (":" + ",".join(dist["AGGRESSIVE"][:8]) if dist["AGGRESSIVE"] else ""),
                 len(dist["OPPORTUNISTIC"]), len(dist["SHADOW"]), len(dist["DISABLED"]), len(rows))
    except Exception as exc:  # noqa: BLE001 — shadow metrics must never affect the cycle
        log.error("PAIR_LIQUIDITY_SHADOW failed (skipped): %s", exc)


async def _market_state_shadow(scan_results, rankings, db, ts, now_epoch):
    """Stage-C market-state spine (SHADOW — measure-only). Computes F1-F5 from live COMPLETED-bar
    data every cycle, logs MARKET_STATE_SHADOW beside the legacy regime label, and persists to
    market_state_log. Books NOTHING, feeds NO decision, changes NO sizing/exit. Never raises into
    the cycle. BTC is COLLECTED as a sensor (not traded)."""
    # Skip on a db without the accessor (test double) — this also avoids the sensor network fetch
    # in unit tests; the real db module always exposes persist_market_state.
    if not hasattr(db, "persist_market_state"):
        return
    try:
        import scanner
        from raid.core import market_state as MS
        by_sym = {sr.symbol: sr for sr in scan_results}
        # BTC sensor (collected, NOT traded): best-effort 5m + 1h.
        btc5 = btc1 = []
        try:
            btc5 = (await scanner.fetch_sensor_ohlcv(["XBTUSD"], interval=5, limit=60)).get("XBTUSD") or []
            btc1 = (await scanner.fetch_sensor_ohlcv(["XBTUSD"], interval=60, limit=60)).get("XBTUSD") or []
        except Exception:  # noqa: BLE001
            pass

        def _major(sym, bars1h):
            h, l, c = MS._ohlc(MS.completed(bars1h))
            atrp = F.atr_pct(h, l, c, 14) if len(c) >= 15 else None
            st = MS._structure(h, l) if len(c) >= 10 else MS.Structure.UNKNOWN
            d = "up" if st == MS.Structure.TREND_UP else "down" if st == MS.Structure.TREND_DOWN else "flat"
            return {"symbol": sym, "atr_1h_pct": atrp, "dir": d}

        majors = []
        if btc1:
            majors.append(_major("BTCUSD", btc1))
        for sym in ("ETHUSD", "SOLUSD"):
            sr = by_sym.get(sym)
            if sr and getattr(sr, "ohlcv_1h", None):
                majors.append(_major(sym, sr.ohlcv_1h))
        breadth = MS.f5_cross_sectional([r.get("return_24h") for r in rankings.values()])

        # Reference series for F2/F3/F4 = the market leader (BTC 5m; else ETH 5m), COMPLETED-bar only.
        if btc5:
            ref_sym, ref_bars = "BTCUSD", MS.completed(btc5, now_epoch)
        else:
            _eth = by_sym.get("ETHUSD")
            ref_sym, ref_bars = "ETHUSD", (MS.completed(_eth.ohlcv, now_epoch) if _eth else [])

        ms = MS.compute_market_state(majors, breadth, ref_bars, ref_sym)

        # Legacy classifier regime for the SAME reference (like-for-like lead/lag comparison).
        legacy = "na"
        try:
            _rb = btc5 if ref_sym == "BTCUSD" else (by_sym[ref_sym].ohlcv if ref_sym in by_sym else [])
            _c = MS.completed(_rb, now_epoch)
            if len(_c) >= 2:
                _h, _l, _cl = MS._ohlc(_c)
                legacy = classify(F.build_feature_snapshot("ms-ref", ref_sym, "5m", _h, _l, _cl)).regime.value
        except Exception:  # noqa: BLE001
            pass

        log.info(
            "MARKET_STATE_SHADOW portfolio=%s fast_dir=%s veto=%s structure=%s | breadth pct_up=%s "
            "median_ret=%s disp=%s n=%s | majors=%s | ref=%s legacy_regime=%s | votes=%s | "
            "thresholds=SEEDED(slope_min=%s crisis_atr1h=%s risk_on=%s risk_off=%s) [calibrate from live dist]",
            ms.portfolio.value, ms.fast_direction.value, ms.excursion_veto, ms.structure.value,
            _pf(breadth.get("pct_up")), _pf(breadth.get("median_return")), _pf(breadth.get("dispersion")),
            breadth.get("n"), [(m["symbol"], m["dir"], _pf(m.get("atr_1h_pct"))) for m in majors],
            ref_sym, legacy, ms.votes, MS.SEED["slope_min"], MS.SEED["crisis_atr_1h_pct"],
            MS.SEED["risk_on_breadth"], MS.SEED["risk_off_breadth"],
        )
        if hasattr(db, "persist_market_state"):
            await db.persist_market_state({
                "cycle_ts": ts, "portfolio_state": ms.portfolio.value,
                "fast_direction": ms.fast_direction.value, "excursion_veto": ms.excursion_veto,
                "structure": ms.structure.value, "breadth_pct_up": breadth.get("pct_up"),
                "breadth_median_return": breadth.get("median_return"),
                "breadth_dispersion": breadth.get("dispersion"), "breadth_n": breadth.get("n"),
                "reference_symbol": ref_sym, "legacy_regime_ref": legacy,
                "majors_json": json.dumps(majors)[:2000], "votes_json": json.dumps(ms.votes)[:1000],
            })
    except Exception as _ms_exc:  # noqa: BLE001 — shadow spine must never affect the cycle
        log.error("MARKET_STATE_SHADOW failed (skipped): %s", _ms_exc)


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

    # Live realized equity (compounds with closed-trade P&L) drives the drawdown ladder + the
    # 95% deployment cap. The 5%-margin SIZING base uses a once-per-day snapshot (smoother).
    equity = float(await db.get_realized_equity())
    sizing_equity = float(await db.get_daily_equity_base())
    open_trades = await db.get_open_trades()
    max_open = int(controls.get("max_open_trades") or config.MAX_OPEN_TRADES)
    peak = max(config.STARTING_EQUITY, equity)
    ts = datetime.now(timezone.utc).isoformat()
    _now_epoch = datetime.now(timezone.utc).timestamp()   # B2: cycle wall-clock for the completed-candle check
    paper_strats = _REGISTRY.paper()

    # (Commit E) The consecutive-loss auto-pause was REMOVED — the bot must not freeze on a
    # normal loss streak. The remaining automated backstops are the drawdown de-risk ladder
    # (_effective_leverage below: 6%->2x, 10%->1x, 15%->pause, 20%->shutdown) and the manual
    # kill_switch (honored by worker._brain_entry_gate). The consec-loss ALERT still fires.

    # --- Leverage + drawdown de-risking (PERSISTED high-water mark; survives restart) ---
    global _peak_equity
    # Seed the high-water mark from the PERSISTED peak (drawdown_state) so a restart/redeploy cannot
    # reset it below the true pre-restart peak — the redeploy-clears-pause bug (B1). Falls back to
    # in-memory when the flag is off or the db lacks the accessor (then identical to the legacy
    # max(_peak_equity, STARTING, equity)); persistence self-disables if the table is absent.
    _persist_dd = config.PERSIST_DRAWDOWN_STATE and hasattr(db, "get_drawdown_state")
    _persisted_peak = 0.0
    if _persist_dd:
        _dd_row = await db.get_drawdown_state()
        if _dd_row:
            _persisted_peak = float(_dd_row.get("peak_equity") or 0.0)
    _peak_equity = resolve_peak(_persisted_peak, _peak_equity, config.STARTING_EQUITY, equity)
    drawdown = (_peak_equity - equity) / _peak_equity if _peak_equity > 0 else 0.0
    eff_lev, halt = _effective_leverage(drawdown)
    _pause_state = ("shutdown" if halt == "shutdown"
                    else "paused" if halt == "pause"
                    else "reduced" if eff_lev != config.LEVERAGE_MULTIPLIER
                    else "none")
    if _persist_dd and hasattr(db, "upsert_drawdown_state"):
        await db.upsert_drawdown_state({
            "peak_equity": _peak_equity, "drawdown_pct": drawdown,
            "leverage_limit": (0 if halt else eff_lev), "pause_state": _pause_state,
        })
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

    # B6 measure-first: aggregate REAL open risk and LOG what the (currently inert, zeroed)
    # portfolio-risk gates WOULD block if fed it. total-open + correlated-cluster exist in
    # risk.assess but the runner feeds PortfolioState(equity, peak) only; same-direction has no gate
    # at all. Measure-only — NOT fed into risk.assess (the enforcement wiring is a later change).
    _tl = TIER_LIMITS[effective_tier(_RISK.base_tier, drawdown)]
    _agg = {"total": 0.0, "long": 0.0, "short": 0.0, "max_cluster": 0.0, "by_cluster": {}}
    try:
        _rt = effective_tier(_RISK.base_tier, drawdown)
        _tl = TIER_LIMITS[_rt]
        _agg = aggregate_open_risk(open_trades, equity, config.CORRELATED_PAIRS)
        log.info(
            "PORTFOLIO_RISK_SHADOW tier=%s open=%d total=%.3f%%/cap%.2f%%%s long=%.3f%% short=%.3f%% "
            "max_cluster=%.3f%%/cap%.2f%%%s — %s",
            _rt.name, len(open_trades), _agg["total"] * 100, _tl.max_total_open_risk_pct * 100,
            " OVER" if _agg["total"] > _tl.max_total_open_risk_pct else "",
            _agg["long"] * 100, _agg["short"] * 100,
            _agg["max_cluster"] * 100, _tl.max_cluster_risk_pct * 100,
            " OVER" if _agg["max_cluster"] > _tl.max_cluster_risk_pct else "",
            "ENFORCED (caps bind)" if config.ENFORCE_PORTFOLIO_RISK else "measure-only (gates fed zeroed state)",
        )
    except Exception as _pr_exc:  # noqa: BLE001 — measurement must never affect the cycle
        log.error("PORTFOLIO_RISK_SHADOW failed (skipped): %s", _pr_exc)
    # B.5: running portfolio risk for the binding caps in the booking loop below — seeded from the
    # real open book, then folded forward as each candidate books so intra-cycle stacking can't breach.
    _run_risk = {"total": _agg["total"], "long": _agg["long"], "short": _agg["short"],
                 "cluster": dict(_agg.get("by_cluster") or {})}

    # --- Cross-sectional universe ranking (computed ONCE per cycle) ---
    rankings = compute_universe_rankings(scan_results)

    # Stage-C market-state spine (SHADOW — measure-only; books nothing, feeds no decision, no sizing/
    # exit change). Logs MARKET_STATE_SHADOW + persists market_state_log alongside the legacy regime.
    await _market_state_shadow(scan_results, rankings, db, ts, _now_epoch)

    # C.6 Appendix-C §2 pair-liquidity metrics (SHADOW — measure-only; feeds no gate yet). Computes
    # the 15 metrics per pair completed-candle + USD-quote, logs a universe summary + the completed-
    # vs-forming volume_ratio shift (B.4), and batch-persists (self-disabling).
    await _pair_liquidity_shadow(scan_results, db, ts, _now_epoch)

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
    # Live concentration counts (per symbol+strategy+direction, and per symbol) for the
    # open-time caps that stop correlated same-symbol stacking. Recomputed each cycle.
    conc_ssd, conc_symbol = open_concentration_counts(open_trades)
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
    _b2_forming = 0   # B2 measure-first: symbols whose latest 5m bar is still forming (would drop)
    _b2_total = 0
    _capture_rows: list = []   # OHLCV backtest capture (write-only; flushed once post-loop)
    # getattr, not db.X: a db WITHOUT the capture API (e.g. a test double, or a partial
    # module) safely disables capture rather than raising in the cycle. Fail-closed.
    _capture_on = bool(getattr(db, "OHLCV_CAPTURE_ENABLED", False))
    for sr in scan_results:
        # B2 measure-first: is the latest 5m bar still forming (would be dropped under completed-
        # candle enforcement)? Log-only — the bar is NOT dropped here (enforcement is a later flip).
        try:
            _wd, _lts, _age = completed_candle_would_drop(sr.ohlcv, _now_epoch)
            _b2_total += 1
            if _wd:
                _b2_forming += 1
                log.info("COMPLETED_CANDLE_WOULD_DROP symbol=%s last_bar_ts=%s age_s=%s tf=5m",
                         sr.symbol, _lts, _age)
        except Exception:  # noqa: BLE001
            pass
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

        # OHLCV capture (write-only backtest instrumentation; NEVER affects trading). Reuses
        # sr.ohlcv already fetched this cycle (no refetch, no new Kraken call). Runs for the
        # full universe here — BEFORE the capacity continues below. Gated OFF by default in db
        # and fully wrapped: a capture problem can never block/delay/crash the trade path.
        if _capture_on:
            try:
                _capture_rows.extend(db.build_ohlcv_capture_rows(sr.symbol, sr.ohlcv))
            except Exception as _cap_exc:  # noqa: BLE001 — capture must never affect the cycle
                log.error("OHLCV capture row-build failed for %s (skipped): %s", sr.symbol, _cap_exc)

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

        # (d) Per-pair eligibility (fail closed): a symbol without Kraken margin capability
        # (absent from config.KRAKEN_MAX_LEVERAGE) cannot be leveraged or shorted live, so it
        # is not booked at all. The 18 priority pairs are all eligible; this guards against a
        # spot-only pair ever re-entering the universe.
        if not is_margin_eligible(sr.symbol):
            log.info("RAID ENGINE: skip %s — not Kraken margin-eligible (fail closed)", sr.symbol)
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

        # Concentration caps (open-time GATE, not a sizing change): reject a candidate that
        # would stack the same (symbol,strategy,direction), or exceed the per-symbol total —
        # the failure mode behind the SLXUSD C3-short 4-stack (~-$20 correlated loss cluster).
        _conc = concentration_reject_reason(
            conc_ssd, conc_symbol, sr.symbol, strat.strategy_id, c.direction.value,
            config.MAX_OPEN_PER_SYMBOL_STRATEGY_DIRECTION, config.MAX_OPEN_PER_SYMBOL_TOTAL,
        )
        if _conc:
            log.info("RAID ENGINE: skip %s %s %s — concentration cap (%s)",
                     sr.symbol, strat.strategy_id, c.direction.value, _conc)
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

        # Graduated cost/R gate (Commit 2) — ATR-scaled-stop strategies ONLY. For C1/C3/C5/C6/C7
        # the honest-TP construction pins net_rr at 1.35 regardless of stop distance, so the
        # net_rr gate above is blind to the ABSOLUTE cost load. When 1R (the stop, = gross_risk)
        # is so tight the ~1.04% round-trip cost dominates it, the trade is structurally
        # unwinnable: reject; half-size the marginal band. Structural strategies (C2/C4/C10) have
        # atr_scaled_stop=False and are exempt (their net_rr gate already prices cost in). This is
        # an ADDITIONAL filter — the net_rr gate still fires independently.
        size_mult = 1.0
        if getattr(strat, "atr_scaled_stop", False):
            _gross_risk = abs(_epx - _sl) / _epx if _epx > 0 else 0.0
            _allow, size_mult, _cr_reason = graduated_size_decision(
                _gross_risk, costs.realized_round_trip_cost_pct(),
                fatal_ratio=config.COST_R_FATAL_RATIO,
                marginal_ratio=config.COST_R_MARGINAL_RATIO,
                marginal_mult=config.COST_R_MARGINAL_SIZE_MULT,
            )
            if not _allow:
                log.info("RAID ENGINE: cost/R reject %s %s — %s", sr.symbol, strat.strategy_id, _cr_reason)
                continue
            if size_mult != 1.0:
                log.info("RAID ENGINE: cost/R half-size %s %s — %s", sr.symbol, strat.strategy_id, _cr_reason)

        # Risk-size, gate, and book the single winning candidate.
        state = PortfolioState(Decimal(str(equity)), Decimal(str(peak)))
        decision = _RISK.assess(state, c.reference_price, c.stop_price)
        if not decision.approved:
            log.info("RAID ENGINE: risk reject %s %s — %s", sr.symbol, strat.strategy_id, decision.reason)
            continue
        # Base = risk-sized notional capped at 5% of the DAILY equity base (compounds day over
        # day). Leverage scales the position notional; margin (= base) counts against the 95%
        # deployment cap (which uses live equity).
        base_notional = min(float(decision.quantity) * float(c.reference_price) * size_mult, config.MAX_TRADE_SIZE_PCT * sizing_equity)
        if base_notional < 10:
            continue
        # Per-pair leverage cap: never exceed Kraken's max for this symbol. 0 => not eligible
        # (fail closed; already filtered above, but re-checked here so leverage can never be
        # applied to an unfit pair).
        pair_lev = capped_leverage(eff_lev, sr.symbol)
        if pair_lev < 1:
            log.info("RAID ENGINE: skip %s — not margin-eligible at book time (fail closed)", sr.symbol)
            continue
        notional = base_notional * pair_lev
        margin = base_notional
        if deployed_margin + margin > equity * config.MAX_EQUITY_DEPLOYED_PCT:
            continue
        log.info("RAID ENGINE: SIZING %s $%.2f notional (%dx leverage, cap %sx, $%.2f margin)",
                 sr.symbol, notional, pair_lev, kraken_max_leverage(sr.symbol), margin)

        # B.5: portfolio-risk caps BIND. Risk-to-stop of THIS candidate on its sized notional; reject
        # if it would push total / same-direction / cluster risk over cap (running risk = real open
        # book + this cycle's prior bookings). _cand_risk/_cidx are also used to fold the booking in.
        _ref = float(c.reference_price)
        _cand_risk = (notional * abs(_ref - float(c.stop_price)) / _ref / equity
                      if (_ref > 0 and equity > 0) else 0.0)
        _cidx = symbol_cluster_index(sr.symbol, config.CORRELATED_PAIRS)
        if config.ENFORCE_PORTFOLIO_RISK:
            _cap = portfolio_cap_reason(
                _cand_risk, c.direction.value, _cidx, _run_risk,
                max_total=_tl.max_total_open_risk_pct, max_same_dir=config.MAX_SAME_DIRECTION_RISK_PCT,
                max_cluster=_tl.max_cluster_risk_pct)
            if _cap:
                log.info("RAID ENGINE: portfolio-risk reject %s %s — %s cap "
                         "(cand=%.3f%% total=%.3f%%/%.2f%% cluster=%.3f%%/%.2f%%)",
                         sr.symbol, c.direction.value, _cap, _cand_risk * 100,
                         _run_risk["total"] * 100, _tl.max_total_open_risk_pct * 100,
                         _run_risk["cluster"].get(_cidx, 0.0) * 100, _tl.max_cluster_risk_pct * 100)
                continue

        sig = Signal(
            market="crypto", symbol=sr.symbol, direction=c.direction.value, confidence=0.0,
            technical_score=0.0, news_sentiment="neutral", news_headline="", news_boost=0.0,
            macro_blocked=False, block_reason="", scan_result=sr,
        )
        g = await gate.check_gate(sig, db, strategy=strat.strategy_id, cycle_ts=ts)
        if not g.passed:
            log.info("RAID ENGINE: gate reject %s — %s", sr.symbol, g.reason)
            continue

        # Instrumentation (Commit 1) — clean immutable risk anchor + entry ATR + conviction
        # inputs, captured once at open. initial_stop_price is NEVER rewritten by the trail
        # (which only mutates `sl`), so it is the clean R denominator for later analysis.
        # entry_atr_pct is the 1h ATR (the atr_scaled_stop basis); NULL if the 1h feature is
        # absent. All are additive columns (migration 003) and change no entry/exit/sizing.
        _f5, _f1h = ctx.feature("5m"), ctx.feature("1h")
        _entry_atr = float(_f1h.atr_pct) if (_f1h is not None and _f1h.atr_pct) else None
        _init_stop_dist = abs(_epx - _sl) / _epx if _epx > 0 else None
        _ema20_dist = ((_epx - _f5.ema20) / _f5.ema20) if (_f5 is not None and _f5.ema20) else None
        _ema50_dist = ((_epx - _f5.ema50) / _f5.ema50) if (_f5 is not None and _f5.ema50) else None
        _entry_slope = (_f5.trend_slope if _f5 is not None else None)
        _vol_ratio = F.volume_ratio(ctx.extras.get("candles_5m"))

        trade = {
            "bot_name": config.BOT_NAME, "market": "crypto", "symbol": sr.symbol,
            "direction": c.direction.value, "entry_price": float(c.reference_price),
            "exit_price": None, "size_usd": round(notional, 2), "confidence": None,
            "pnl": 0, "status": "open", "close_reason": None, "paper_mode": config.PAPER_MODE,
            "sl": float(c.stop_price), "tp": float(c.targets[0]),
            "instrument_type": "crypto", "market_regime": ctx.market_regime.value,
            "claude_reasoning": f"{strat.strategy_id} {c.entry_type.value} net_rr={c.net_rr} lev={pair_lev}x margin={margin:.2f} :: {strat.explain_decision(c, ctx)}"[:1000],
            "predicted_prob": None, "kelly_fraction": None,
            "initial_stop_price": float(c.stop_price), "initial_stop_distance_pct": _init_stop_dist,
            "entry_atr_pct": _entry_atr, "ema20_dist_pct": _ema20_dist, "ema50_dist_pct": _ema50_dist,
            "entry_slope": _entry_slope, "volume_ratio": _vol_ratio,
        }
        tid = await db.log_trade(trade)
        if tid:
            booked += 1
            deployed_margin += margin
            open_symbols.add(sr.symbol)
            # B.5: fold this booking into the running portfolio risk so a later candidate this cycle
            # sees it (total/same-dir/cluster caps bind intra-cycle).
            _run_risk["total"] += _cand_risk
            _run_risk["long" if c.direction.value in ("long", "yes") else "short"] += _cand_risk
            if _cidx is not None:
                _run_risk["cluster"][_cidx] = _run_risk["cluster"].get(_cidx, 0.0) + _cand_risk
            # Keep the live concentration counts current so a later symbol this same cycle
            # (or a repeat) cannot slip past the caps.
            conc_symbol[sr.symbol] = conc_symbol.get(sr.symbol, 0) + 1
            _k = (sr.symbol, strat.strategy_id, c.direction.value)
            conc_ssd[_k] = conc_ssd.get(_k, 0) + 1
            open_dirs_by_symbol.setdefault(sr.symbol, set()).add(c.direction.value)
            log.info("RAID ENGINE: booked %s %s %s $%.2f sl=%.6f tp=%.6f net_rr=%s",
                     strat.strategy_id, sr.symbol, c.direction.value, notional,
                     float(c.stop_price), float(c.targets[0]), c.net_rr)
            # B4: record the versioned dynamic cost estimate for this trade (cost_estimates, new
            # table; NOT an ALTER trades). RECORD-ONLY — the gate/P&L still use the flat 1.04% floor.
            if hasattr(db, "insert_cost_estimate"):
                try:
                    _ce = costs.dynamic_round_trip_cost_pct(spread_pct=float(ctx.spread_pct or 0.0))
                    _ce.update({"trade_id": tid, "pair": sr.symbol, "direction": c.direction.value})
                    await db.insert_cost_estimate(_ce)
                except Exception as _ce_exc:  # noqa: BLE001 — recording must never affect the cycle
                    log.error("cost_estimate record failed for %s (skipped): %s", sr.symbol, _ce_exc)

    # Flush this cycle's OHLCV capture in ONE batched write. Guarded by _capture_on (so a db
    # without the capture API is skipped) and wrapped (capture_ohlcv_5m already never raises)
    # — the trade cycle is already complete above and is unaffected by the result.
    if _capture_on and _capture_rows:
        try:
            _captured = await db.capture_ohlcv_5m(_capture_rows)
            if _captured:
                log.info("RAID ENGINE: OHLCV capture wrote %d rows", _captured)
        except Exception as _cap_exc:  # noqa: BLE001 — capture must never affect the cycle
            log.error("OHLCV capture flush failed (cycle unaffected): %s", _cap_exc)

    log.info("COMPLETED_CANDLE_SUMMARY forming=%d/%d (B2 measure-first; bars NOT dropped)",
             _b2_forming, _b2_total)
    log.info(
        "RAID ENGINE: cycle complete — booked %d (regimes: %s | produced: %s | "
        "shadow: c7_shorts=%d c10_sweeps=%d c10_shadow=%d)",
        booked, regime_tally or "none", produced_by or "none",
        shadow_tally["c7_shorts"], shadow_tally["c10_sweeps"], shadow_tally["c10_shadow"],
    )
    return booked
