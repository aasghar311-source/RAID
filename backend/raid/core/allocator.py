"""Capital allocator — expectancy-weighted with shrinkage (Section 11.3).

Allocates portfolio weight toward strategies with statistically supported positive
NET expectancy. Small samples are shrunk toward zero so a lucky 5-trade streak does
not command capital. Never allocates on win-rate alone, gross profit alone, or LLM
confidence. Cash is a valid allocation: weights sum to at most MAX_DEPLOY_FRACTION.
"""

from __future__ import annotations

from dataclasses import dataclass

# Shrinkage strength: expectancy is multiplied by n/(n+SHRINKAGE_K). At n=K the
# estimate is halved; large samples are barely shrunk.
SHRINKAGE_K = 30
MIN_SAMPLE = 20                 # below this, a strategy gets zero allocation
MAX_WEIGHT_PER_STRATEGY = 0.40  # no single strategy dominates the book
MAX_DEPLOY_FRACTION = 1.00      # remainder (>=0) is held as cash


@dataclass(frozen=True)
class StrategyStats:
    strategy_id: str
    sample_size: int
    net_expectancy: float   # net $ per trade (after all costs)
    profit_factor: float    # gross wins / gross losses; 1.0 = breakeven
    max_drawdown_pct: float  # 0..1


@dataclass(frozen=True)
class Allocation:
    weights: dict[str, float]     # strategy_id -> fraction of equity
    cash_weight: float
    notes: tuple[str, ...]


def _score(s: StrategyStats) -> float:
    """Shrunk, drawdown-penalized expectancy score. Zero for ineligible strategies."""
    if s.sample_size < MIN_SAMPLE:
        return 0.0
    if s.net_expectancy <= 0 or s.profit_factor <= 1.0:
        return 0.0
    shrink = s.sample_size / (s.sample_size + SHRINKAGE_K)
    shrunk_exp = s.net_expectancy * shrink
    # Penalize deep drawdowns (halve the score at 20% dd, zero by 50%).
    dd_penalty = max(0.0, 1.0 - s.max_drawdown_pct / 0.50)
    # Mild profit-factor bonus (capped so it can't dominate expectancy).
    pf_bonus = min(s.profit_factor, 2.0) / 2.0
    return shrunk_exp * dd_penalty * pf_bonus


def allocate(stats: list[StrategyStats]) -> Allocation:
    notes: list[str] = []
    scores = {s.strategy_id: _score(s) for s in stats}
    eligible = {k: v for k, v in scores.items() if v > 0}

    if not eligible:
        notes.append("no_strategy_with_positive_shrunk_expectancy -> 100% cash")
        return Allocation({}, 1.0, tuple(notes))

    total = sum(eligible.values())
    raw = {k: (v / total) * MAX_DEPLOY_FRACTION for k, v in eligible.items()}

    # Cap per-strategy weight, redistributing the overflow to cash (conservative —
    # does not force capital onto other strategies).
    weights: dict[str, float] = {}
    for k, w in raw.items():
        if w > MAX_WEIGHT_PER_STRATEGY:
            notes.append(f"{k} capped {w:.3f}->{MAX_WEIGHT_PER_STRATEGY}")
            weights[k] = MAX_WEIGHT_PER_STRATEGY
        else:
            weights[k] = w

    deployed = sum(weights.values())
    cash = max(0.0, 1.0 - deployed)
    return Allocation(weights, cash, tuple(notes))
