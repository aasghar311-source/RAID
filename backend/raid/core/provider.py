"""Market-data provider abstraction (Section 13).

Kraken is the authoritative venue. The engine talks to a MarketDataProvider, never
to Kraken directly, so alternate/historical sources can supplement without silently
replacing live data. The raw->normalized converters are pure and unit-tested here;
the live network methods are wired to the existing scanner at the Phase-5 cutover
(kept unwired now so the legacy bot is untouched).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from raid.core.marketdata import NormalizedBar, OrderBookSnapshot

# Capability tokens a strategy may require and a provider/account may grant.
CAP_SPOT_LONG = "spot_long"
CAP_SHORT = "short"
CAP_MARGIN = "margin"
CAP_FUTURES = "futures"


def normalize_kraken_ohlc(raw: list) -> list[NormalizedBar]:
    """Kraken OHLC row: [time, open, high, low, close, vwap, volume, count].
    Skips malformed rows rather than fabricating values (fail closed per-row)."""
    bars: list[NormalizedBar] = []
    for row in raw or []:
        try:
            bars.append(NormalizedBar(
                ts=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[6]),
            ))
        except (IndexError, TypeError, ValueError):
            continue
    return bars


def normalize_kraken_orderbook(raw: dict, ts: int) -> OrderBookSnapshot:
    """Kraken Depth result: {'bids': [[price, vol, ts], ...], 'asks': [...]}. Bids
    come descending, asks ascending; we preserve that order."""
    def _side(rows):
        out = []
        for r in rows or []:
            try:
                out.append((float(r[0]), float(r[1])))
            except (IndexError, TypeError, ValueError):
                continue
        return tuple(out)

    return OrderBookSnapshot(
        ts=ts,
        bids=_side((raw or {}).get("bids")),
        asks=_side((raw or {}).get("asks")),
    )


class MarketDataProvider(ABC):
    """Async venue data interface. Implementations must never fabricate data — on
    failure they raise, and the caller takes the no-trade path."""

    name: str = "abstract"

    @abstractmethod
    def capabilities(self) -> frozenset[str]:
        """Trading capabilities this provider/account currently supports."""

    @abstractmethod
    async def get_bars(self, symbol: str, timeframe: str, limit: int) -> list[NormalizedBar]:
        ...

    @abstractmethod
    async def get_order_book(self, symbol: str, depth: int) -> OrderBookSnapshot:
        ...

    @abstractmethod
    async def get_ticker(self, symbol: str) -> float:
        ...


class KrakenProvider(MarketDataProvider):
    """Authoritative live provider. Network methods are wired to scanner at the
    Phase-5 cutover; capabilities reflect the current spot-paper posture."""

    name = "kraken"

    def capabilities(self) -> frozenset[str]:
        # Spot long only until short/margin capability is verified + operator-enabled.
        return frozenset({CAP_SPOT_LONG})

    async def get_bars(self, symbol: str, timeframe: str, limit: int) -> list[NormalizedBar]:
        raise NotImplementedError("wired to scanner at Phase-5 cutover")

    async def get_order_book(self, symbol: str, depth: int) -> OrderBookSnapshot:
        raise NotImplementedError("wired to scanner at Phase-5 cutover")

    async def get_ticker(self, symbol: str) -> float:
        raise NotImplementedError("wired to scanner at Phase-5 cutover")
