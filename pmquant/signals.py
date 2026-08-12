"""Signal engine: each signal maps order-book state to a directional score or trade idea.

Signals implemented:
- imbalance: depth-weighted buy/sell pressure near the touch
- momentum:  drift of the mid-price over recent snapshots
- dutch_book: YES + NO asks summing below $1 → risk-free arbitrage pair
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .models import OrderBook


@dataclass
class ArbOpportunity:
    """Buy YES and NO for a combined price < $1; guaranteed $1 at resolution."""
    ask_yes: float
    ask_no: float
    edge: float          # 1 - (ask_yes + ask_no) - fees, per share pair
    max_shares: float    # min top-of-book depth across both legs


def imbalance_score(book: OrderBook, within: float = 0.05) -> Optional[float]:
    """Order-book imbalance in [-1, 1]; positive means net buy pressure."""
    return book.imbalance(within)


def momentum_score(mids: List[float], lookback: int = 10) -> Optional[float]:
    """Mid-price drift over the last `lookback` snapshots, in price units."""
    if len(mids) < lookback:
        return None
    return mids[-1] - mids[-lookback]


def dutch_book(book_yes: OrderBook, book_no: OrderBook, fee_per_share: float = 0.0,
               min_edge: float = 0.005) -> Optional[ArbOpportunity]:
    """Detect a dutch book: both asks buyable for less than the $1 payout."""
    if not book_yes.asks or not book_no.asks:
        return None
    ay, an = book_yes.asks[0].price, book_no.asks[0].price
    edge = 1.0 - (ay + an) - 2 * fee_per_share
    if edge < min_edge:
        return None
    return ArbOpportunity(
        ask_yes=ay, ask_no=an, edge=edge,
        max_shares=min(book_yes.asks[0].size, book_no.asks[0].size),
    )
