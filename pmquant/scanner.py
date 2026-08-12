"""Market scanner: find liquid, active binary markets worth tracking."""
from __future__ import annotations

from typing import List

from .api import GammaClient
from .models import Market


def scan(gamma: GammaClient, top: int = 20, min_liquidity: float = 10_000,
         min_volume24h: float = 5_000, max_price_extreme: float = 0.97) -> List[Market]:
    """Return up to `top` markets ranked by 24h volume.

    Filters out illiquid markets and near-resolved ones (price already pinned
    at 0/1), where there is no edge left to trade.
    """
    selected: List[Market] = []
    offset = 0
    while len(selected) < top and offset < 500:
        batch = gamma.active_markets(limit=100, offset=offset)
        if not batch:
            break
        offset += len(batch)
        for payload in batch:
            m = Market.from_gamma(payload)
            if m is None or m.closed:
                continue
            if m.liquidity < min_liquidity or m.volume24h < min_volume24h:
                continue
            if m.outcome_prices and max(m.outcome_prices) > max_price_extreme:
                continue
            selected.append(m)
            if len(selected) >= top:
                break
    return selected
