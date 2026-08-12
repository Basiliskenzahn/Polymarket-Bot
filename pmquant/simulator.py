"""Paper broker: simulates fills against the live order book.

Market orders walk the book level by level, so simulated fills pay real
slippage. Fees use Polymarket's schedule: fee = rate * min(p, 1-p) * shares
(the base rate is currently 0 on most markets, but the model is configurable
so results stay honest if fees return).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import Level, OrderBook


@dataclass
class Fill:
    shares: float
    avg_price: float
    fee: float
    slippage: float  # avg fill price vs pre-trade mid, signed against the trader


@dataclass
class Position:
    token_id: str
    market_id: str
    shares: float = 0.0
    cost: float = 0.0          # total cash paid including fees
    entry_ts: float = field(default_factory=time.time)
    signal: str = ""

    @property
    def avg_price(self) -> float:
        return self.cost / self.shares if self.shares else 0.0


class FeeModel:
    def __init__(self, rate: float = 0.0):
        self.rate = rate

    def fee(self, price: float, shares: float) -> float:
        return self.rate * min(price, 1.0 - price) * shares


class InsufficientLiquidity(RuntimeError):
    pass


def _walk(levels: List[Level], shares: float) -> float:
    """Cost of taking `shares` from the book, walking levels best-first."""
    remaining, cost = shares, 0.0
    for level in levels:
        take = min(remaining, level.size)
        cost += take * level.price
        remaining -= take
        if remaining <= 1e-9:
            return cost
    raise InsufficientLiquidity(f"book too thin for {shares} shares")


class PaperBroker:
    def __init__(self, cash: float = 1_000.0, fee_model: Optional[FeeModel] = None):
        self.cash = cash
        self.fees = fee_model or FeeModel()
        self.positions: Dict[str, Position] = {}
        self.realized_pnl = 0.0
        self.fees_paid = 0.0

    def position(self, token_id: str) -> Optional[Position]:
        return self.positions.get(token_id)

    def buy(self, book: OrderBook, market_id: str, shares: float,
            signal: str = "") -> Fill:
        cost = _walk(book.asks, shares)
        avg = cost / shares
        fee = self.fees.fee(avg, shares)
        if cost + fee > self.cash:
            raise InsufficientLiquidity("insufficient paper cash")
        self.cash -= cost + fee
        self.fees_paid += fee
        pos = self.positions.setdefault(
            book.token_id, Position(book.token_id, market_id, signal=signal))
        pos.shares += shares
        pos.cost += cost + fee
        mid = book.mid or avg
        return Fill(shares, avg, fee, avg - mid)

    def sell(self, book: OrderBook, shares: Optional[float] = None) -> Fill:
        pos = self.positions.get(book.token_id)
        if pos is None or pos.shares <= 0:
            raise InsufficientLiquidity("no position to sell")
        shares = pos.shares if shares is None else min(shares, pos.shares)
        proceeds = 0.0
        remaining = shares
        for level in book.bids:
            take = min(remaining, level.size)
            proceeds += take * level.price
            remaining -= take
            if remaining <= 1e-9:
                break
        filled = shares - remaining
        if filled <= 0:
            raise InsufficientLiquidity("no bids to sell into")
        avg = proceeds / filled
        fee = self.fees.fee(avg, filled)
        cost_basis = pos.avg_price * filled
        self.cash += proceeds - fee
        self.fees_paid += fee
        self.realized_pnl += proceeds - fee - cost_basis
        pos.cost -= cost_basis
        pos.shares -= filled
        if pos.shares <= 1e-9:
            del self.positions[book.token_id]
        mid = book.mid or avg
        return Fill(filled, avg, fee, mid - avg)

    def settle(self, token_id: str, payout_per_share: float) -> float:
        """Resolve a position at 0 or 1 when the market closes."""
        pos = self.positions.pop(token_id, None)
        if pos is None:
            return 0.0
        proceeds = pos.shares * payout_per_share
        self.cash += proceeds
        pnl = proceeds - pos.cost
        self.realized_pnl += pnl
        return pnl

    def position_value(self, mids: Dict[str, float]) -> float:
        return sum(p.shares * mids.get(t, p.avg_price)
                   for t, p in self.positions.items())
