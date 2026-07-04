"""RAID cost model — the SINGLE authoritative source for all trade cost accounting.

Every fee, spread, and slippage assumption lives here so the ledger can record a
full cost breakdown (gross, entry_fee, exit_fee, spread, slippage, financing, net)
instead of one blended number. This replaces scattered magic numbers across the
codebase: executor.compute_pnl and the trailing fee-floors now both derive from
realized_round_trip_cost_pct() here (was 0.0016*2 and entry*1.004 / entry*0.996).

TWO COST FUNCTIONS (deliberately separate):
  * REALIZED cost - realized_round_trip_cost_pct(). This is the SINGLE cost used by BOTH
    the live P&L (executor.compute_pnl, runner._rotation_pnl) AND the live entry gate
    (raid.strategies.helpers.build_candidate computes net_rr from it). Reflects THIS
    account's real tier: all-taker 0.40%/side x2 + ~0.02% margin-open + spread + slippage
    ~= 1.04% round-trip on notional.
  * LEGACY PLANNING helpers - compute_costs()/net_rr()/round_trip_cost_pct() default to
    ASSUMED_FILL_FEE_PCT (0.16%). These have NO production callers (tests only) and do NOT
    feed any live entry gate. Retained for the legacy brain path / reference only.

Verified fee model (operator read the tier live from the Kraken Pro order bar): base tier
0.25% maker / 0.40% taker per side. The engine fills are ALL taker (immediate market entries
+ stop/market exits; no resting-limit path). The realized ledger now charges the taker rate;
before ANY live activation, re-confirm the account tier and per-fill maker/taker split.
"""

from dataclasses import dataclass

# THIS ACCOUNT'S REAL Kraken fee tier (operator-verified live from the Pro order bar):
# base tier 0.25% maker / 0.40% taker per side. The engine's fills are ALL TAKER (immediate
# market entries + stop/market exits; there is no resting-limit path), so the realized
# ledger uses the TAKER rate. (Kraken's published *lowest*-volume tier is 0.16%/0.26% — the
# legacy assumption — but this account trades at the base tier.)
KRAKEN_TAKER_FEE_PCT = 0.0040   # 0.40% per side — account base tier; engine fills are taker
KRAKEN_MAKER_FEE_PCT = 0.0025   # 0.25% per side — reference only; the engine never rests a limit
MARGIN_OPEN_FEE_PCT  = 0.0002   # ~0.02% charged once on notional when a margin position opens

# Execution costs the perfect-fill sim omitted (were 0). SLIPPAGE from Phase-1B (~0.17% avg
# measured on real stop exits); SPREAD a conservative bid/ask-crossing estimate (~0.05%, ~ the
# runner's order-book default floor rounded up for illiquid microcaps). NOTE: exit slippage is
# PARTLY already reflected in the realized exit_price, so charging SLIPPAGE_PCT again makes the
# all-in figure slightly conservative (pessimistic) — the safe direction for a paper sim.
SPREAD_PCT   = 0.0005
SLIPPAGE_PCT = 0.0017

# Legacy 0.16% fill assumption. NOT used by the live entry gate: build_candidate
# (raid/strategies/helpers.py) computes net_rr from realized_round_trip_cost_pct() (~1.04%),
# the SAME cost the P&L charges. ASSUMED_FILL_FEE_PCT only defaults the legacy planning
# helpers below (compute_costs/net_rr/round_trip_cost_pct), which have no production callers
# (tests only). Retained for the legacy brain path / reference.
ASSUMED_FILL_FEE_PCT = 0.0016

# Perfect-fill paper defaults for the PLANNING cost model (compute_costs/net_rr); the realized
# ledger uses realized_round_trip_cost_pct() below instead.
DEFAULT_SPREAD_PCT = 0.0
DEFAULT_SLIPPAGE_PCT = 0.0


def realized_round_trip_cost_pct() -> float:
    """All-in round-trip execution cost as a fraction of NOTIONAL (size_usd) for a TAKER
    round trip on this account's tier: taker fee x2 (both legs) + one margin-open fee +
    spread + slippage. This is the REALIZED-ledger cost used by executor.compute_pnl and
    runner._rotation_pnl. Separate from the frozen planning cost (round_trip_cost_pct)."""
    return 2.0 * KRAKEN_TAKER_FEE_PCT + MARGIN_OPEN_FEE_PCT + SPREAD_PCT + SLIPPAGE_PCT


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
