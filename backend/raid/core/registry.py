"""Strategy registry — the single place that knows which strategies exist and their
runtime mode (disabled / shadow / paper / quarantined). Feature-flagged so any
strategy can be turned off independently without code changes.

Promotion/quarantine transitions are driven by the Phase-6 evidence engine; this
registry only holds and guards the state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from raid.core.strategy import Strategy, StrategyMode


@dataclass
class RegistryEntry:
    strategy: Strategy
    mode: StrategyMode
    enabled: bool = True
    notes: list[str] = field(default_factory=list)


class StrategyRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, RegistryEntry] = {}

    def register(self, strategy: Strategy, mode: StrategyMode = StrategyMode.SHADOW, enabled: bool = True) -> None:
        sid = strategy.strategy_id
        if sid in self._entries:
            raise ValueError(f"strategy '{sid}' already registered")
        if sid == "abstract":
            raise ValueError("cannot register the abstract base strategy")
        self._entries[sid] = RegistryEntry(strategy=strategy, mode=mode, enabled=enabled)

    def get(self, strategy_id: str) -> Strategy:
        return self._entries[strategy_id].strategy

    def mode(self, strategy_id: str) -> StrategyMode:
        return self._entries[strategy_id].mode

    def set_mode(self, strategy_id: str, mode: StrategyMode) -> None:
        self._entries[strategy_id].mode = mode

    def set_enabled(self, strategy_id: str, enabled: bool) -> None:
        self._entries[strategy_id].enabled = enabled

    def all(self) -> list[Strategy]:
        return [e.strategy for e in self._entries.values()]

    def active(self) -> list[Strategy]:
        """Strategies that may run this cycle: enabled and not disabled."""
        return [
            e.strategy for e in self._entries.values()
            if e.enabled and e.mode != StrategyMode.DISABLED
        ]

    def paper(self) -> list[Strategy]:
        """Strategies allowed to consume paper capital (mode == PAPER, enabled)."""
        return [
            e.strategy for e in self._entries.values()
            if e.enabled and e.mode == StrategyMode.PAPER
        ]

    def ids(self) -> list[str]:
        return list(self._entries.keys())

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, strategy_id: str) -> bool:
        return strategy_id in self._entries
