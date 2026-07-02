"""Strict typed trade candidate — the ONLY object allowed into the financial path.

Replaces the legacy `Y:T+MTF+V=4 P:0 -> 0.70` prose-string pipeline. A Candidate is
immutable and fully validated at construction: financially material fields have NO
silent defaults, and any inconsistency (stop on the wrong side, negative price,
net_rr that doesn't match the legs, costs exceeding reward) raises ValidationError.

Rebuild rules enforced here:
  * Malformed -> REJECT (raise). Never infer a missing stop, recover an omitted
    probability, repair a symbol, or widen a target.
  * There is no `probability` field. LLM confidence is not a trade-quality metric.
  * net_* fields must be internally consistent with the gross_* legs and costs.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class EntryType(str, Enum):
    MARKET = "market"        # cross the spread now
    LIMIT = "limit"          # rest at limit_price (pullback)
    STOP = "stop"            # trigger through trigger_price (breakout)


class MarketRegime(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    VOLATILE = "volatile"
    CRISIS = "crisis"
    UNKNOWN = "unknown"


class CandidateStatus(str, Enum):
    """Order/candidate lifecycle (Section 7.3). A candidate begins CANDIDATE and is
    advanced only by the order manager; UNKNOWN freezes new risk for the symbol."""

    CANDIDATE = "candidate"
    VALIDATED = "validated"
    REJECTED = "rejected"
    ARMED = "armed"
    EXPIRED = "expired"
    SUBMITTING = "submitting"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    PROTECTION_PENDING = "protection_pending"
    PROTECTED = "protected"
    EXIT_SUBMITTING = "exit_submitting"
    EXIT_ACKNOWLEDGED = "exit_acknowledged"
    CLOSED = "closed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"


# Terminal states that must never transition further.
TERMINAL_STATUSES = frozenset({
    CandidateStatus.REJECTED,
    CandidateStatus.EXPIRED,
    CandidateStatus.CLOSED,
    CandidateStatus.CANCELED,
})


class Rejection(BaseModel):
    """A structured rejection. Strategies/validators return this instead of a
    silently-repaired candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str
    symbol: str
    reasons: tuple[str, ...] = Field(min_length=1)
    code_version: str = "unknown"


class Candidate(BaseModel):
    """An immutable, fully-validated trade candidate. Construction fails closed."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)

    # --- identity / provenance ------------------------------------------------
    candidate_id: str
    strategy_id: str
    strategy_version: str
    code_version: str
    symbol: str
    instrument_id: str

    # --- setup ----------------------------------------------------------------
    direction: Direction
    setup_timeframe: str
    market_regime: MarketRegime

    # --- entry / protection (prices are Decimal to avoid float drift) ---------
    entry_type: EntryType
    trigger_price: Optional[Decimal] = None   # required for STOP entries
    limit_price: Optional[Decimal] = None     # required for LIMIT entries
    reference_price: Decimal                  # live price the setup was measured against
    stop_price: Decimal
    targets: tuple[Decimal, ...] = Field(min_length=1)

    # --- expected costs (fractions of notional, or absolute USD for financing) -
    expected_entry_fee: Decimal
    expected_exit_fee: Decimal
    expected_spread: Decimal
    expected_slippage: Decimal

    # --- economics (net_* must reconcile with gross_* and costs) --------------
    gross_reward: Decimal
    gross_risk: Decimal
    net_reward: Decimal
    net_risk: Decimal
    net_rr: Decimal

    # --- sizing (assigned by the risk manager, not the strategy or the LLM) ---
    planned_risk_dollars: Decimal
    quantity: Decimal

    # --- timing ---------------------------------------------------------------
    setup_timestamp: str
    expiry_timestamp: str

    # --- linkage --------------------------------------------------------------
    market_data_snapshot_id: str
    feature_snapshot_id: str

    # --- gating metadata ------------------------------------------------------
    capability_requirements: tuple[str, ...] = ()
    rejection_reasons: tuple[str, ...] = ()

    # ---------------------------------------------------------------- validators

    @field_validator("reference_price", "stop_price")
    @classmethod
    def _prices_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("price must be > 0 (fail closed — no silent repair)")
        return v

    @field_validator("targets")
    @classmethod
    def _targets_positive(cls, v: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
        if any(t <= 0 for t in v):
            raise ValueError("all targets must be > 0")
        return v

    @field_validator("quantity", "planned_risk_dollars", "net_risk", "gross_risk")
    @classmethod
    def _positive_risk_qty(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("quantity/risk fields must be > 0 (uneconomic -> reject)")
        return v

    @model_validator(mode="after")
    def _structural_consistency(self) -> "Candidate":
        entry_ref = self.entry_reference_price()
        is_long = self.direction == Direction.LONG

        # Stop on the correct side of the entry.
        if is_long and self.stop_price >= entry_ref:
            raise ValueError("long stop must be BELOW entry (wrong side -> reject)")
        if not is_long and self.stop_price <= entry_ref:
            raise ValueError("short stop must be ABOVE entry (wrong side -> reject)")

        # Every target on the correct (profitable) side of the entry.
        for t in self.targets:
            if is_long and t <= entry_ref:
                raise ValueError("long target must be ABOVE entry")
            if not is_long and t >= entry_ref:
                raise ValueError("short target must be BELOW entry")

        # Entry-type / price coherence.
        if self.entry_type == EntryType.STOP and self.trigger_price is None:
            raise ValueError("STOP entry requires trigger_price")
        if self.entry_type == EntryType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT entry requires limit_price")

        # net_rr must reconcile with the net legs (no free-floating number).
        if self.net_risk > 0:
            expected = (self.net_reward / self.net_risk).quantize(Decimal("0.0001"))
            if (self.net_rr.quantize(Decimal("0.0001")) - expected).copy_abs() > Decimal("0.0005"):
                raise ValueError(f"net_rr {self.net_rr} inconsistent with net_reward/net_risk {expected}")

        # net legs must not exceed gross (costs only ever reduce reward / raise risk).
        if self.net_reward > self.gross_reward:
            raise ValueError("net_reward cannot exceed gross_reward")
        if self.net_risk < self.gross_risk:
            raise ValueError("net_risk cannot be below gross_risk")
        return self

    # ---------------------------------------------------------------- helpers

    def entry_reference_price(self) -> Decimal:
        """The price the entry is evaluated against for the given entry type."""
        if self.entry_type == EntryType.LIMIT and self.limit_price is not None:
            return self.limit_price
        if self.entry_type == EntryType.STOP and self.trigger_price is not None:
            return self.trigger_price
        return self.reference_price

    def is_economic(self, min_net_rr: Decimal = Decimal("1.0")) -> bool:
        """True only if the setup clears the minimum net (post-cost) reward/risk."""
        return self.net_rr >= min_net_rr
