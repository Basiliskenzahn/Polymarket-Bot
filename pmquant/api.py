"""HTTP clients for Polymarket's public Gamma (metadata) and CLOB (order book) APIs.

Both APIs are public for market data — no authentication required.
Cloudflare rejects the default urllib User-Agent, hence the browser-like header.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) pmquant/0.1",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


class ApiError(RuntimeError):
    """Raised when a request fails after all retries."""


def _request(url: str, body: Optional[bytes] = None, retries: int = 3,
             timeout: float = 15.0) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise ApiError(f"{url} failed after {retries} attempts: {last_error}")


def _get(base: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _request(url)


class GammaClient:
    """Market metadata: questions, outcomes, volume, liquidity, resolution state."""

    def active_markets(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        return _get(GAMMA_BASE, "/markets", {
            "active": "true", "closed": "false",
            "limit": limit, "offset": offset,
            "order": "volume24hr", "ascending": "false",
        })

    def market(self, market_id: str) -> Dict[str, Any]:
        return _get(GAMMA_BASE, f"/markets/{market_id}")


class ClobClient:
    """Order book data from the central limit order book."""

    def book(self, token_id: str) -> Dict[str, Any]:
        return _get(CLOB_BASE, "/book", {"token_id": token_id})

    def books(self, token_ids: List[str]) -> List[Dict[str, Any]]:
        """Batch fetch order books; falls back to sequential GETs on failure."""
        body = json.dumps([{"token_id": t} for t in token_ids]).encode()
        try:
            return _request(CLOB_BASE + "/books", body=body)
        except ApiError:
            return [self.book(t) for t in token_ids]

    def midpoint(self, token_id: str) -> float:
        return float(_get(CLOB_BASE, "/midpoint", {"token_id": token_id})["mid"])
