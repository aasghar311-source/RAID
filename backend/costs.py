"""RAID cost model — the SINGLE authoritative source for all trade cost accounting.

Every fee, spread, and slippage assumption lives here so the ledger can record a
full cost breakdown (gross, entry_fee, exit_fee, spread, slippage, financing, net)
instead of one blended number. This replaces scattered magic numbers across the
codebase: executor.compute_pnl (0.0016*2), brain prompt (0.0032), the UI (0.0032),
and the trailing fee-floors (entry*1.004 / entry*0.996).

DROP-IN COMPATIBILITY: net_pnl() with defaults reproduces executor.compute_pnl()
exactly (maker 0.16%/side round trip), so wiring this into the live path does not
change any historical or current number. Spread/slippage/financing default to 0
(the current "perfect paper fill" assumption); Phase 5's fill simulator will supply
real values.

⚠️ FEE-RATE CAVEAT (Aug-14 readiness gate §25 "verified fee model"):
Kraken's published lowest-30d-volume-tier fees are MAKER 0.16%/side and TAKER
0.26%/side. The legacy paper model assumed 0.16% for EVERY fill (i.e. treats all
fills as maker), which is optimistic for stop/market exits that are really taker
fills at 0.26%. We keep 0.16% as the default here ONLY to preserve ledger continuity
with the 441 historical trades. Before ANY live activation the real account fee tier
and per-fill maker/taker classification MUST be verified and modeled. Do not silently
change the default rate — that is a modeling decision for the operator.
"""

from dataclasses import dataclass

# Kraken published lowest-tier fees (per side). Accurate reference values.
KRAKEN_MAKER_FEE_PCT = 0.0016   # 0.16% per side
KRAKEN_TAKER_FEE_PCT = 0.0026   # 0.26% per side

# The paper model's current assumption: all fills treated as maker (matches the
# legacy executor.compute_pnl). Documented so it can be revisited, not buried.
ASSUMED_FILL_FEE_PCT = KRAKEN_MAKER_FEE_PCT

# Perfect-fill paper defaults; Phase 5 fill simulator overrides these per trade.
DEFAULT_SPREAD_PCT = 0.0
DEFAULT_SLIPPAGE_PCT = 0.0


@dataclass(frozen=True)
class CostBreakdown:
    """Itemized cost accounting for one round-trip trade. All fields in USD."""

    gross_pnl: float
    entry_fee: float
    exit_fee: float
    spread_cost: float
    slippage_cost: float
    financing_cost: float

    @property
    def total_cost(self) -> float:
        return (
            self.entry_fee
            + self.exit_fee
            + self.spread_cost
            + self.slippage_cost
            + self.financing_cost
        )

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.total_cost

    def as_dict(self) -> dict:
        """Flat dict for persistence into a cost-itemized ledger."""
        return {
            "gross_pnl": self.gross_pnl,
            "entry_fee": self.entry_fee,
            "exit_fee": self.exit_fee,
            "spread_cost": self.spread_cost,
            "slippage_cost": self.slippage_cost,
            "financing_cost": self.financing_cost,
            "total_cost": self.total_cost,
            "net_pnl": self.net_pnl,
        }


def _is_long(direction: str) -> bool:
    return direction in ("long", "yes")


def gross_pnl(direction: str, entry: float, exit_price: float, size_usd: float) -> float:
    """Gross USD PnL before any costs. Raises on non-positive entry (fail closed —
    never silently return 0 for a malformed price)."""
    if entry <= 0:
        raise ValueError(f"entry price must be > 0, got {entry}")
    move = (exit_price - entry) / entry if _is_long(direction) else (entry - exit_price) / entry
    return size_usd * move


def compute_costs(
    direction: str,
    entry: float,
    exit_price: float,
    size_usd: float,
    *,
    fee_pct: float = ASSUMED_FILL_FEE_PCT,
    spread_pct: float = DEFAULT_SPREAD_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    financing_cost: float = 0.0,
) -> CostBreakdown:
    """Full itemized cost breakdown for a round-trip trade.

    fee_pct is charged on BOTH entry and exit (per side). spread_pct and slippage_pct
    are charged once each on notional. financing_cost is an absolute USD figure
    (borrow/funding), supplied by short/carry strategies; 0 for spot longs.
    """
    g = gross_pnl(direction, entry, exit_price, size_usd)
    entry_fee = size_usd * fee_pct
    exit_fee = size_usd * fee_pct
    spread_cost = size_usd * spread_pct
    slippage_cost = size_usd * slippage_pct
    return CostBreakdown(g, entry_fee, exit_fee, spread_cost, slippage_cost, financing_cost)


def net_pnl(
    direction: str,
    entry: float,
    exit_price: float,
    size_usd: float,
    **kwargs,
) -> float:
    """Realized USD PnL net of all costs. With defaults this equals the legacy
    executor.compute_pnl (gross - size*0.0016*2)."""
    return compute_costs(direction, entry, exit_price, size_usd, **kwargs).net_pnl


def round_trip_cost_pct(
    *,
    fee_pct: float = ASSUMED_FILL_FEE_PCT,
    spread_pct: float = DEFAULT_SPREAD_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
) -> float:
    """Total round-trip cost as a fraction of notional (both fees + spread + slippage)."""
    return 2.0 * fee_pct + spread_pct + slippage_pct


def net_rr(
    entry: float,
    stop: float,
    target: float,
    *,
    fee_pct: float = ASSUMED_FILL_FEE_PCT,
    spread_pct: float = DEFAULT_SPREAD_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
):
    """Net reward-to-risk after costs. Returns None when the setup is uneconomic
    (net risk non-positive). Never widens the target — reports the true net R:R so
    callers can REJECT rather than repair (rebuild rule: no auto-widening)."""
    if entry <= 0:
        raise ValueError(f"entry price must be > 0, got {entry}")
    reward_move = abs(target - entry) / entry
    risk_move = abs(entry - stop) / entry
    cost = round_trip_cost_pct(fee_pct=fee_pct, spread_pct=spread_pct, slippage_pct=slippage_pct)
    net_reward = reward_move - cost
    net_risk = risk_move + cost
    if net_risk <= 0:
        return None
    return net_reward / net_risk
