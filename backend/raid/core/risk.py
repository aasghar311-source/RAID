"""Portfolio risk manager — deterministic risk tiers, drawdown de-risking, and
position sizing from a risk budget (Section 11).

AI has no authority here. Risk is a pure function of realized equity, drawdown, and
configured tier limits. The 1.50% absolute per-trade ceiling is enforced as a final
clamp and can only be raised by an explicit operator change to HARD_CEILING_PCT.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import IntEnum


class RiskTier(IntEnum):
    SHADOW = 0        # research only, no portfolio capital
    INITIAL = 1
    VALIDATED = 2
    STRONG = 3
    AGGRESSIVE = 4
    EXCEPTIONAL = 5   # requires explicit operator approval; 1.50% ceiling


@dataclass(frozen=True)
class TierLimits:
    risk_per_trade_pct: float
    max_total_open_risk_pct: float
    max_cluster_risk_pct: float


TIER_LIMITS: dict[RiskTier, TierLimits] = {
    RiskTier.SHADOW:      TierLimits(0.0000, 0.0000, 0.0000),
    RiskTier.INITIAL:     TierLimits(0.0050, 0.0300, 0.0150),
    RiskTier.VALIDATED:   TierLimits(0.0075, 0.0400, 0.0200),
    RiskTier.STRONG:      TierLimits(0.0100, 0.0500, 0.0250),
    RiskTier.AGGRESSIVE:  TierLimits(0.0125, 0.0600, 0.0300),
    RiskTier.EXCEPTIONAL: TierLimits(0.0150, 0.0700, 0.0350),
}

# Absolute per-trade hard ceiling. Do not raise without explicit operator change.
HARD_CEILING_PCT = 0.0150

# Drawdown de-risk ladder (§11.2). Fractions of peak realized equity.
DD_DERISK_ONE_TIER = 0.06
DD_DERISK_TO_TIER1 = 0.10
DD_PAUSE_ENTRIES = 0.15
DD_HARD_SHUTDOWN = 0.20

# Loss-streak pauses.
DAILY_LOSS_PAUSE_PCT = 0.04
WEEKLY_LOSS_PAUSE_PCT = 0.08


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    effective_tier: RiskTier
    risk_dollars: Decimal
    quantity: Decimal


def effective_tier(base_tier: RiskTier, drawdown_pct: float) -> RiskTier:
    """Apply the drawdown de-risk ladder. Never raises the tier."""
    if drawdown_pct >= DD_DERISK_TO_TIER1:
        return min(base_tier, RiskTier.INITIAL)
    if drawdown_pct >= DD_DERISK_ONE_TIER:
        return RiskTier(max(RiskTier.INITIAL, base_tier - 1)) if base_tier > RiskTier.INITIAL else base_tier
    return base_tier


def clamped_risk_pct(tier: RiskTier) -> float:
    """Per-trade risk % for a tier, never above the hard ceiling."""
    return min(TIER_LIMITS[tier].risk_per_trade_pct, HARD_CEILING_PCT)


def position_size(equity: Decimal, risk_pct: float, entry: Decimal, stop: Decimal) -> tuple[Decimal, Decimal]:
    """Return (risk_dollars, quantity) such that a stop-out loses exactly risk_pct of
    equity (before costs). Fail closed on a zero/degenerate stop distance."""
    if entry <= 0:
        raise ValueError("entry must be > 0")
    stop_dist = abs(entry - stop) / entry
    if stop_dist <= 0:
        raise ValueError("stop distance must be > 0 (degenerate -> reject)")
    risk_dollars = equity * Decimal(str(risk_pct))
    notional = risk_dollars / Decimal(str(stop_dist))
    quantity = notional / entry
    return risk_dollars, quantity


def graduated_size_decision(
    gross_risk: float,
    rt_cost: float,
    *,
    fatal_ratio: float,
    marginal_ratio: float,
    marginal_mult: float,
) -> tuple[bool, float, str]:
    """Graduated cost/R gate on the realized stop distance (gross_risk = 1R).

    cost/R = rt_cost / gross_risk. Reject when cost/R >= fatal_ratio (the round-trip cost
    dominates the risk unit -> structurally unwinnable); half (marginal_mult) when
    cost/R is in [marginal_ratio, fatal_ratio); full size otherwise. Returns
    (allow, size_mult, reason). Pure — thresholds/cost are injected by the caller so this
    is trivially testable and never drifts from the fee SSOT. Applies ONLY to the
    ATR-scaled-stop strategies (the caller scopes it via Strategy.atr_scaled_stop)."""
    if gross_risk <= 0:
        return (False, 0.0, "degenerate_stop")
    fatal_stop = rt_cost / fatal_ratio          # gross_risk below this => cost/R >= fatal
    marginal_stop = rt_cost / marginal_ratio    # gross_risk below this => cost/R >= marginal
    if gross_risk < fatal_stop:
        return (False, 0.0, f"cost/R fatal: gross_risk={gross_risk:.4f}<{fatal_stop:.4f}")
    if gross_risk < marginal_stop:
        return (True, marginal_mult, f"cost/R marginal: gross_risk={gross_risk:.4f}<{marginal_stop:.4f} -> x{marginal_mult}")
    return (True, 1.0, "cost/R ok")


def aggregate_open_risk(open_trades, equity, correlated_groups=None) -> dict:
    """B6 measure-first: aggregate REAL open risk from current open positions so the (currently
    inert, zeroed) portfolio-risk gates can be sized. Per trade, dollar risk-to-stop = size_usd *
    |entry - sl| / entry (skipped when data is missing). Returns fractions of equity: total, long,
    short, and the max correlated-cluster. Pure; feeds NO decision (measurement only)."""
    eq = float(equity or 0.0)
    if eq <= 0:
        return {"total": 0.0, "long": 0.0, "short": 0.0, "max_cluster": 0.0}
    sym_cluster: dict = {}
    for i, grp in enumerate(correlated_groups or []):
        for s in grp:
            sym_cluster[s] = i
    total = long = short = 0.0
    cluster: dict = {}
    for t in open_trades or []:
        try:
            entry = float(t.get("entry_price") or 0.0)
            sl = float(t.get("sl") or 0.0)
            size = float(t.get("size_usd") or 0.0)
            if entry <= 0 or sl <= 0 or size <= 0:
                continue
            risk_d = size * abs(entry - sl) / entry
        except (TypeError, ValueError):
            continue
        total += risk_d
        if t.get("direction") in ("long", "yes"):
            long += risk_d
        else:
            short += risk_d
        ci = sym_cluster.get(t.get("symbol"))
        if ci is not None:
            cluster[ci] = cluster.get(ci, 0.0) + risk_d
    max_cluster = max(cluster.values()) if cluster else 0.0
    return {"total": total / eq, "long": long / eq, "short": short / eq, "max_cluster": max_cluster / eq}


@dataclass
class PortfolioState:
    equity: Decimal
    peak_equity: Decimal
    open_risk_pct: float = 0.0        # sum of open planned risk / equity
    cluster_risk_pct: float = 0.0     # risk in the candidate's correlation cluster
    daily_loss_pct: float = 0.0       # today's realized loss / equity (>=0)
    weekly_loss_pct: float = 0.0

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, float((self.peak_equity - self.equity) / self.peak_equity))


class PortfolioRiskManager:
    def __init__(self, base_tier: RiskTier = RiskTier.INITIAL):
        self.base_tier = base_tier

    def system_halted(self, state: PortfolioState) -> str | None:
        """Return a halt reason if NO new risk may be taken, else None."""
        dd = state.drawdown_pct
        if dd >= DD_HARD_SHUTDOWN:
            return f"hard_shutdown_drawdown_{dd:.3f}>=0.20"
        if dd >= DD_PAUSE_ENTRIES:
            return f"pause_entries_drawdown_{dd:.3f}>=0.15"
        if state.daily_loss_pct >= DAILY_LOSS_PAUSE_PCT:
            return f"daily_loss_pause_{state.daily_loss_pct:.3f}>=0.04"
        if state.weekly_loss_pct >= WEEKLY_LOSS_PAUSE_PCT:
            return f"weekly_loss_pause_{state.weekly_loss_pct:.3f}>=0.08"
        return None

    def assess(self, state: PortfolioState, entry: Decimal, stop: Decimal) -> RiskDecision:
        halt = self.system_halted(state)
        if halt:
            return RiskDecision(False, halt, RiskTier.SHADOW, Decimal(0), Decimal(0))

        tier = effective_tier(self.base_tier, state.drawdown_pct)
        limits = TIER_LIMITS[tier]
        if tier == RiskTier.SHADOW or limits.risk_per_trade_pct <= 0:
            return RiskDecision(False, "shadow_tier_no_capital", tier, Decimal(0), Decimal(0))

        risk_pct = clamped_risk_pct(tier)

        # Portfolio-level exposure gates BEFORE sizing.
        if state.open_risk_pct + risk_pct > limits.max_total_open_risk_pct + 1e-9:
            return RiskDecision(False, f"max_total_open_risk_{limits.max_total_open_risk_pct}", tier, Decimal(0), Decimal(0))
        if state.cluster_risk_pct + risk_pct > limits.max_cluster_risk_pct + 1e-9:
            return RiskDecision(False, f"max_cluster_risk_{limits.max_cluster_risk_pct}", tier, Decimal(0), Decimal(0))

        try:
            risk_dollars, quantity = position_size(state.equity, risk_pct, entry, stop)
        except ValueError as exc:
            return RiskDecision(False, f"sizing_failed:{exc}", tier, Decimal(0), Decimal(0))

        return RiskDecision(True, "approved", tier, risk_dollars, quantity)
