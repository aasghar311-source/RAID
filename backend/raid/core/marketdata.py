"""Normalized market-data layer + data-quality validation (Section 13).

Raw venue payloads are normalized into typed bars/order-books, then validated. Bad
data does not raise into the strategy path — it produces DataQualityEvents and an
`is_usable()` verdict, so the engine can choose the no-trade path (Section 9) instead
of trading on a broken candle or a crossed book.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"   # data unusable — no-trade


@dataclass(frozen=True)
class DataQualityEvent:
    severity: Severity
    symbol: str
    kind: str
    detail: str


@dataclass(frozen=True)
class NormalizedBar:
    ts: int      # unix seconds, bar open time
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class OrderBookSnapshot:
    ts: int
    bids: tuple[tuple[float, float], ...]   # (price, size), descending price
    asks: tuple[tuple[float, float], ...]   # (price, size), ascending price

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_pct(self) -> float | None:
        b, a, m = self.best_bid, self.best_ask, self.mid
        if b is None or a is None or not m:
            return None
        return (a - b) / m

    def depth_usd(self, side: str, levels: int = 5) -> float:
        book = self.bids if side == "bid" else self.asks
        return sum(p * s for p, s in book[:levels])


@dataclass(frozen=True)
class MarketDataSnapshot:
    snapshot_id: str
    symbol: str
    ts: int
    bars_by_tf: dict[str, tuple[NormalizedBar, ...]]
    order_book: OrderBookSnapshot | None = None
    events: tuple[DataQualityEvent, ...] = field(default_factory=tuple)

    def is_usable(self) -> bool:
        return not any(e.severity == Severity.CRITICAL for e in self.events)


def validate_bars(symbol: str, timeframe: str, bars: list[NormalizedBar]) -> list[DataQualityEvent]:
    """Check a bar series for the failures that corrupt indicators: too few bars,
    non-positive/misordered OHLC, duplicate or out-of-order timestamps, gaps."""
    ev: list[DataQualityEvent] = []
    if len(bars) < 2:
        ev.append(DataQualityEvent(Severity.CRITICAL, symbol, "insufficient_bars", f"{timeframe}:{len(bars)}"))
        return ev

    prev_ts = None
    step = None
    for i, b in enumerate(bars):
        if min(b.open, b.high, b.low, b.close) <= 0:
            ev.append(DataQualityEvent(Severity.CRITICAL, symbol, "nonpositive_price", f"{timeframe}@{b.ts}"))
        if b.high < max(b.open, b.close, b.low) or b.low > min(b.open, b.close, b.high):
            ev.append(DataQualityEvent(Severity.CRITICAL, symbol, "ohlc_inconsistent", f"{timeframe}@{b.ts}"))
        if prev_ts is not None:
            if b.ts <= prev_ts:
                ev.append(DataQualityEvent(Severity.CRITICAL, symbol, "ts_not_increasing", f"{timeframe}@{b.ts}<=prev{prev_ts}"))
            else:
                gap = b.ts - prev_ts
                if step is None:
                    step = gap
                elif gap != step:
                    ev.append(DataQualityEvent(Severity.WARN, symbol, "irregular_bar_gap", f"{timeframe}:{gap}!=step{step}"))
        prev_ts = b.ts
    return ev


def validate_order_book(symbol: str, ob: OrderBookSnapshot | None, max_spread_pct: float = 0.01) -> list[DataQualityEvent]:
    ev: list[DataQualityEvent] = []
    if ob is None or not ob.bids or not ob.asks:
        ev.append(DataQualityEvent(Severity.CRITICAL, symbol, "empty_order_book", "no_bids_or_asks"))
        return ev
    if ob.best_bid is not None and ob.best_ask is not None and ob.best_bid >= ob.best_ask:
        ev.append(DataQualityEvent(Severity.CRITICAL, symbol, "crossed_book", f"bid{ob.best_bid}>=ask{ob.best_ask}"))
    sp = ob.spread_pct
    if sp is not None and sp > max_spread_pct:
        ev.append(DataQualityEvent(Severity.WARN, symbol, "wide_spread", f"{sp:.4f}>{max_spread_pct}"))
    return ev
