"""Realistic paper fill simulator (Section 15).

Models what the legacy engine assumed away: taker fills walk real order-book depth
(VWAP + slippage), limit orders fill only if the market trades to them, depth can be
insufficient (partial fill), an empty book rejects the order, and protective stops can
gap THROUGH their level (filled worse than the stop). No order is assumed to fill.
"""

from __future__ import annotations

from dataclasses import dataclass

from raid.core.marketdata import OrderBookSnapshot

MAKER_FEE_PCT = 0.0016
TAKER_FEE_PCT = 0.0026


@dataclass(frozen=True)
class FillResult:
    filled_qty: float
    avg_price: float
    fee_paid: float
    slippage_cost: float
    is_partial: bool
    rejected: bool
    reason: str

    @property
    def notional(self) -> float:
        return self.filled_qty * self.avg_price


def _reject(reason: str) -> FillResult:
    return FillResult(0.0, 0.0, 0.0, 0.0, False, True, reason)


def simulate_taker(book: OrderBookSnapshot, qty: float, side: str,
                   fee_pct: float = TAKER_FEE_PCT) -> FillResult:
    """Market order: walk the book (asks for a buy, bids for a sell). Partial-fills
    when depth is insufficient; rejects on an empty book."""
    if qty <= 0:
        return _reject("nonpositive_qty")
    levels = book.asks if side == "buy" else book.bids
    if not levels:
        return _reject("empty_book")
    best = levels[0][0]
    remaining = qty
    cost = 0.0
    for price, size in levels:
        take = min(remaining, size)
        cost += take * price
        remaining -= take
        if remaining <= 1e-12:
            break
    filled = qty - remaining
    if filled <= 0:
        return _reject("no_liquidity")
    avg = cost / filled
    # Slippage vs the best price (adverse for both sides by construction of the walk).
    slippage = abs(avg - best) * filled
    fee = avg * filled * fee_pct
    return FillResult(filled, avg, fee, slippage, is_partial=filled < qty - 1e-12,
                      rejected=False, reason="taker_filled")


def simulate_maker_limit(qty: float, limit_price: float, side: str, touched: bool,
                         available_qty: float | None = None,
                         fee_pct: float = MAKER_FEE_PCT) -> FillResult:
    """Resting limit order: fills at limit_price only if the market traded to it
    (`touched`). Optional `available_qty` caps the fill (partial). No slippage — the
    price is certain — but not every limit fills."""
    if qty <= 0 or limit_price <= 0:
        return _reject("bad_limit_params")
    if not touched:
        return _reject("limit_not_touched")
    fill = qty if available_qty is None else min(qty, available_qty)
    if fill <= 0:
        return _reject("no_resting_liquidity")
    fee = limit_price * fill * fee_pct
    return FillResult(fill, limit_price, fee, 0.0, is_partial=fill < qty - 1e-12,
                      rejected=False, reason="maker_filled")


def simulate_stop_exit(stop_price: float, market_price: float, qty: float, side: str,
                       fee_pct: float = TAKER_FEE_PCT) -> FillResult:
    """Protective stop exit. Fills as a taker at the WORSE of the stop and the current
    market (models gap-through-stop slippage). `side` is the exit side: 'sell' closes a
    long (fills at min(stop, market)); 'buy' closes a short (fills at max(stop, market))."""
    if qty <= 0 or stop_price <= 0 or market_price <= 0:
        return _reject("bad_stop_params")
    if side == "sell":            # closing a long: worse = lower
        fill_price = min(stop_price, market_price)
        slip = (stop_price - fill_price) * qty
    else:                         # closing a short: worse = higher
        fill_price = max(stop_price, market_price)
        slip = (fill_price - stop_price) * qty
    fee = fill_price * qty * fee_pct
    gapped = abs(fill_price - stop_price) > 1e-12
    return FillResult(qty, fill_price, fee, slip, is_partial=False, rejected=False,
                      reason="stop_gapped" if gapped else "stop_at_level")
