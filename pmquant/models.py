"""Core data structures: markets and order books."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Level:
    price: float
    size: float


@dataclass
class OrderBook:
    """Normalized order book: bids best-first (descending), asks best-first (ascending).

    The CLOB API returns levels sorted worst-to-best, so we re-sort on ingest.
    """
    token_id: str
    bids: List[Level] = field(default_factory=list)
    asks: List[Level] = field(default_factory=list)
    tick_size: float = 0.01
    timestamp: float = field(default_factory=time.time)

    @classmethod
    def from_api(cls, payload: Dict[str, Any]) -> "OrderBook":
        bids = sorted((Level(float(l["price"]), float(l["size"])) for l in payload.get("bids") or []),
                      key=lambda l: -l.price)
        asks = sorted((Level(float(l["price"]), float(l["size"])) for l in payload.get("asks") or []),
                      key=lambda l: l.price)
        return cls(
            token_id=payload["asset_id"],
            bids=bids,
            asks=asks,
            tick_size=float(payload.get("tick_size") or 0.01),
        )

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.bids and self.asks:
            return self.asks[0].price - self.bids[0].price
        return None

    def depth(self, side: str, within: float = 0.05) -> float:
        """Total size on one side within `within` of the best price."""
        levels = self.bids if side == "bid" else self.asks
        if not levels:
            return 0.0
        best = levels[0].price
        return sum(l.size for l in levels if abs(l.price - best) <= within)

    def imbalance(self, within: float = 0.05) -> Optional[float]:
        """(bid_depth - ask_depth) / total in [-1, 1]; positive = buy pressure."""
        b, a = self.depth("bid", within), self.depth("ask", within)
        if b + a == 0:
            return None
        return (b - a) / (b + a)


@dataclass
class Market:
    """A binary prediction market with YES/NO outcome tokens."""
    id: str
    question: str
    slug: str
    condition_id: str
    end_date: str
    outcomes: List[str]
    token_ids: List[str]
    liquidity: float
    volume24h: float
    closed: bool = False
    outcome_prices: List[float] = field(default_factory=list)

    @classmethod
    def from_gamma(cls, payload: Dict[str, Any]) -> Optional["Market"]:
        """Parse a Gamma API market; returns None for non-binary or untradeable markets.

        `outcomes`, `outcomePrices` and `clobTokenIds` arrive as JSON-encoded strings.
        """
        try:
            outcomes = json.loads(payload.get("outcomes") or "[]")
            token_ids = json.loads(payload.get("clobTokenIds") or "[]")
            prices = [float(p) for p in json.loads(payload.get("outcomePrices") or "[]")]
        except (json.JSONDecodeError, ValueError):
            return None
        if len(token_ids) != 2 or len(outcomes) != 2:
            return None
        return cls(
            id=str(payload["id"]),
            question=payload.get("question", ""),
            slug=payload.get("slug", ""),
            condition_id=payload.get("conditionId", ""),
            end_date=payload.get("endDate", ""),
            outcomes=outcomes,
            token_ids=token_ids,
            liquidity=float(payload.get("liquidity") or 0),
            volume24h=float(payload.get("volume24hr") or 0),
            closed=bool(payload.get("closed")),
            outcome_prices=prices,
        )
