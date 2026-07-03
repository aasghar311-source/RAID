"""RAID Omega engine runner — the deterministic strategy cycle wired into the worker.

Replaces the legacy LLM brain path (behind config.USE_NEW_ENGINE). REUSES the proven
plumbing: scanner for market data, gate for risk gates, db for persistence, and
executor.monitor_positions for exits (SL/TP/MAT/trail on the trades table). This module
only owns the DECISION layer: data -> features -> regime -> strategies -> typed
candidate -> risk-sized -> gated -> booked paper trade.

Booking model: a paper strategy fires only when its entry is actionable now (price
already at resistance/support), so the candidate is booked at the reference price this
cycle. A trigger-based fill via the state machine + fill simulator is a follow-up.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

import config
import gate
from signals import Signal

from raid.core import features as F
from raid.core.provider import CAP_SPOT_LONG
from raid.core.regime import classify
from raid.core.risk import PortfolioRiskManager, PortfolioState, RiskTier
from raid.core.strategy import StrategyContext, StrategyMode
from raid.strategies.catalog import build_default_registry

log = logging.getLogger("raid.runner")

# Strategies with real single-symbol logic run PAPER; the rest stay SHADOW until their
# data/capability contract is met (promotion to PAPER is otherwise evidence-gated).
_PAPER_ON_CUTOVER = ("RAID-C1", "RAID-C2", "RAID-C4", "RAID-C5")
_RISK = PortfolioRiskManager(RiskTier.INITIAL)   # Tier 1: 0.5% risk/trade at cutover
_CAPABILITIES = frozenset({CAP_SPOT_LONG})       # spot long only in paper


def build_cutover_registry():
    reg = build_default_registry()
    for sid in _PAPER_ON_CUTOVER:
        reg.set_mode(sid, StrategyMode.PAPER)
    return reg


_REGISTRY = build_cutover_registry()


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


def _context(sr, equity: float, ts: str):
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
    return StrategyContext(
        symbol=sr.symbol, instrument_id=sr.symbol, timestamp=ts,
        market_regime=classify(feats["5m"]).regime, features=feats,
        market_data_snapshot_id=f"{sr.symbol}-{ts}", reference_price=px,
        spread_pct=spread, depth_ok=depth_ok, capabilities=_CAPABILITIES,
        extras={"equity": float(equity), "risk_pct": 0.005, "expiry_ts": ts},
    )


async def _hold_lease(db) -> bool:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    expiry_iso = now.replace(microsecond=0).isoformat()
    # Renew/acquire with a 60s TTL.
    from datetime import timedelta
    new_expiry = (now + timedelta(seconds=60)).isoformat()
    return await db.try_claim_lease(1, config.WORKER_ID, now_iso, new_expiry)


async def run_strategy_cycle(scan_results, db, controls: dict) -> int:
    """One deterministic cycle. Returns the number of paper trades booked."""
    log.info("RAID ENGINE: strategy cycle start — %d symbols", len(scan_results))

    if not await _hold_lease(db):
        log.warning("RAID ENGINE: another worker holds the lease — running passive (no bookings)")
        return 0

    equity = float(await db.get_equity() or config.STARTING_EQUITY)
    open_trades = await db.get_open_trades()
    deployed = sum(float(t.get("size_usd") or 0) for t in open_trades)
    max_open = int(controls.get("max_open_trades") or config.MAX_OPEN_TRADES)
    peak = max(config.STARTING_EQUITY, equity)
    ts = datetime.now(timezone.utc).isoformat()
    paper_strats = _REGISTRY.paper()

    booked = 0
    regime_tally: dict[str, int] = {}
    for sr in scan_results:
        if len(open_trades) + booked >= max_open or booked >= config.MAX_ENTRIES_PER_CYCLE:
            break
        if deployed >= equity * config.MAX_EQUITY_DEPLOYED_PCT:
            break
        ctx = _context(sr, equity, ts)
        if ctx is None:
            continue
        regime_tally[ctx.market_regime.value] = regime_tally.get(ctx.market_regime.value, 0) + 1

        # (a) Persist the per-symbol regime so the Regimes dashboard populates.
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

        # Collect candidates from every eligible paper strategy for this symbol.
        symbol_cands = []
        for strat in paper_strats:
            if not strat.is_eligible(ctx):
                continue
            for c in strat.generate_candidates(ctx):
                symbol_cands.append((strat, c))
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

        # Risk-size, gate, and book the single winning candidate.
        state = PortfolioState(Decimal(str(equity)), Decimal(str(peak)))
        decision = _RISK.assess(state, c.reference_price, c.stop_price)
        if not decision.approved:
            log.info("RAID ENGINE: risk reject %s %s — %s", sr.symbol, strat.strategy_id, decision.reason)
            continue
        notional = min(float(decision.quantity) * float(c.reference_price), config.MAX_TRADE_SIZE_PCT * equity)
        if notional < 10 or deployed + notional > equity * config.MAX_EQUITY_DEPLOYED_PCT:
            continue

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
            "claude_reasoning": f"{strat.strategy_id} {c.entry_type.value} net_rr={c.net_rr} :: {strat.explain_decision(c, ctx)}"[:1000],
            "predicted_prob": None, "kelly_fraction": None,
        }
        tid = await db.log_trade(trade)
        if tid:
            booked += 1
            deployed += notional
            log.info("RAID ENGINE: booked %s %s %s $%.2f sl=%.6f tp=%.6f net_rr=%s",
                     strat.strategy_id, sr.symbol, c.direction.value, notional,
                     float(c.stop_price), float(c.targets[0]), c.net_rr)

    log.info("RAID ENGINE: cycle complete — booked %d (regimes: %s)", booked, regime_tally or "none")
    return booked
