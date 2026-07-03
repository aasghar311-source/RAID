"""Order state machine (Section 7.3).

Enforces the legal lifecycle so the engine can never treat a submission as a fill or a
local record as venue truth. Illegal transitions raise. UNKNOWN freezes new risk for
the symbol until reconciliation resolves it. Every transition is appended to an
immutable history for the audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from raid.core.candidate import CandidateStatus as S
from raid.core.candidate import TERMINAL_STATUSES

# Legal forward transitions. Anything not listed is illegal (raises).
_LEGAL: dict[S, frozenset[S]] = {
    S.CANDIDATE: frozenset({S.VALIDATED, S.REJECTED}),
    S.VALIDATED: frozenset({S.ARMED, S.REJECTED, S.EXPIRED}),
    S.ARMED: frozenset({S.SUBMITTING, S.EXPIRED, S.CANCELED}),
    S.SUBMITTING: frozenset({S.ACKNOWLEDGED, S.UNKNOWN}),          # never -> FILLED directly
    S.ACKNOWLEDGED: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.UNKNOWN}),
    S.PARTIALLY_FILLED: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.UNKNOWN}),
    S.FILLED: frozenset({S.PROTECTION_PENDING, S.UNKNOWN}),
    S.PROTECTION_PENDING: frozenset({S.PROTECTED, S.UNKNOWN}),
    S.PROTECTED: frozenset({S.EXIT_SUBMITTING, S.CLOSED, S.UNKNOWN}),
    S.EXIT_SUBMITTING: frozenset({S.EXIT_ACKNOWLEDGED, S.UNKNOWN}),
    S.EXIT_ACKNOWLEDGED: frozenset({S.CLOSED, S.PARTIALLY_FILLED, S.UNKNOWN}),
    # UNKNOWN must be reconciled before anything else can happen.
    S.UNKNOWN: frozenset({S.RECONCILIATION_REQUIRED}),
    S.RECONCILIATION_REQUIRED: frozenset({S.PROTECTED, S.CLOSED, S.CANCELED, S.ARMED}),
    # terminal states: no outgoing transitions
    S.REJECTED: frozenset(),
    S.EXPIRED: frozenset(),
    S.CLOSED: frozenset(),
    S.CANCELED: frozenset(),
}

# States in which NO new risk may be taken for the symbol.
_RISK_FROZEN = frozenset({S.UNKNOWN, S.RECONCILIATION_REQUIRED})


class IllegalTransition(Exception):
    pass


@dataclass(frozen=True)
class Transition:
    frm: S
    to: S
    reason: str


@dataclass
class OrderStateMachine:
    order_id: str
    status: S = S.CANDIDATE
    history: list[Transition] = field(default_factory=list)

    def can(self, to: S) -> bool:
        return to in _LEGAL.get(self.status, frozenset())

    def transition(self, to: S, reason: str = "") -> None:
        if self.status in TERMINAL_STATUSES:
            raise IllegalTransition(f"{self.order_id}: {self.status.value} is terminal")
        if not self.can(to):
            raise IllegalTransition(
                f"{self.order_id}: illegal {self.status.value} -> {to.value}"
            )
        self.history.append(Transition(self.status, to, reason))
        self.status = to

    def to_unknown(self, reason: str) -> None:
        """Force UNKNOWN from any non-terminal state (venue/ack ambiguity). Always
        legal from a live order so uncertainty can never be silently ignored."""
        if self.status in TERMINAL_STATUSES:
            raise IllegalTransition(f"{self.order_id}: {self.status.value} is terminal")
        self.history.append(Transition(self.status, S.UNKNOWN, reason))
        self.status = S.UNKNOWN

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def risk_frozen(self) -> bool:
        """True when this order's state forbids taking new risk on the symbol."""
        return self.status in _RISK_FROZEN

    @property
    def is_filled(self) -> bool:
        return self.status in (S.FILLED, S.PROTECTION_PENDING, S.PROTECTED,
                               S.EXIT_SUBMITTING, S.EXIT_ACKNOWLEDGED, S.CLOSED)
