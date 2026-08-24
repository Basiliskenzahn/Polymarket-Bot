"""Binance spot market data over the public websocket (no auth required).

Reuses the hand-rolled RFC 6455 client and emits events in the SAME shape as
the Polymarket feed (`book` / `last_trade_price` dicts keyed by asset_id), so
TickStore delta-encoding, the flow study, and the maker backtest all work on
crypto data unchanged.

Streams per symbol: `<sym>@depth5@1000ms` (top-5 book snapshot each second —
delta-encoded downstream, like Polymarket's full-book snapshots were) and
`<sym>@aggTrade` (aggregated trade prints; `m` = buyer-is-maker flags the
aggressor side).
"""
from __future__ import annotations

import json
import socket
import threading
import time
from typing import Callable, Dict, List, Optional

from .api import _get
from .models import Level, OrderBook
from .ws import WebSocketClient

REST_BASE = "https://api.binance.com"
WS_BASE = "wss://stream.binance.com:443/stream?streams="

_EXCLUDE_BASES = ("USDC", "FDUSD", "TUSD", "DAI", "EUR", "BUSD", "USDP", "EURI")

# Force a reconnect if the socket is open but silent this long. depth5@1000ms
# guarantees at least one event per symbol per second, so 15s of silence means
# the connection is dead (laptop sleep, NAT drop) even if TCP hasn't noticed.
STALE_AFTER = 15.0


def top_symbols(n: int = 5) -> List[str]:
    """Top USDT pairs by 24h quote volume, excluding stablecoin pairs."""
    tickers = _get(REST_BASE, "/api/v3/ticker/24hr")
    usdt = []
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if (base in _EXCLUDE_BASES or "USD" in base
                or base.endswith(("UP", "DOWN", "BULL", "BEAR"))):
            continue
        usdt.append((float(t["quoteVolume"]), sym))
    usdt.sort(reverse=True)
    return [sym for _, sym in usdt[:n]]


class BinanceStream:
    """Live top-5 books + trade prints for a set of symbols."""

    def __init__(self, symbols: List[str],
                 on_event: Optional[Callable] = None):
        self.symbols = [s.upper() for s in symbols]
        self._sink = on_event
        self.connected = False
        self._last_event = 0.0
        self._lock = threading.Lock()
        self._books: Dict[str, dict] = {}   # sym -> {"bids": [...], "asks": [...], "ts": float}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _url(self) -> str:
        streams = []
        for s in self.symbols:
            streams.append(f"{s.lower()}@depth5@1000ms")
            streams.append(f"{s.lower()}@aggTrade")
        return WS_BASE + "/".join(streams)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def data_age(self) -> float:
        """Seconds since the last message from the socket (inf if never).

        `connected` only says the last handshake succeeded; a half-dead TCP
        connection (laptop sleep, NAT drop) keeps it True while nothing
        arrives. Freshness is the honest health signal.
        """
        last = self._last_event
        return time.time() - last if last else float("inf")

    def book(self, symbol: str, max_age: float = 30.0) -> Optional[OrderBook]:
        with self._lock:
            state = self._books.get(symbol.upper())
            if state is None or time.time() - state["ts"] > max_age:
                return None
            bids = [Level(float(p), float(q)) for p, q in state["bids"] if float(q) > 0]
            asks = [Level(float(p), float(q)) for p, q in state["asks"] if float(q) > 0]
        if not bids and not asks:
            return None
        return OrderBook(token_id=symbol.upper(), bids=bids, asks=asks,
                         tick_size=0.0)

    # -- reader thread ---------------------------------------------------------

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            ws = WebSocketClient(self._url())
            try:
                ws.connect()
                self.connected = True
                backoff = 1.0
                last_pong = time.time()
                self._last_event = time.time()
                while not self._stop.is_set():
                    if time.time() - last_pong > 60:
                        ws.send_pong()  # unsolicited pong keepalive per docs
                        last_pong = time.time()
                    try:
                        msg = ws.recv_message()
                    except socket.timeout:
                        if self.data_age() > STALE_AFTER:
                            break  # open but silent socket: force reconnect
                        continue
                    if msg is None:
                        break
                    self._last_event = time.time()
                    self._handle(msg)
            except Exception:
                pass
            finally:
                self.connected = False
                ws.close()
            if not self._stop.is_set():
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _handle(self, raw: str) -> None:
        try:
            wrapper = json.loads(raw)
        except ValueError:
            return
        stream = wrapper.get("stream", "")
        data = wrapper.get("data")
        if not isinstance(data, dict):
            return
        now = time.time()
        if "@depth" in stream:
            symbol = stream.split("@", 1)[0].upper()
            bids = data.get("bids") or []
            asks = data.get("asks") or []
            with self._lock:
                self._books[symbol] = {"bids": bids, "asks": asks, "ts": now}
            if self._sink is not None:
                try:
                    self._sink(now, {
                        "event_type": "book",
                        "asset_id": symbol,
                        "bids": [{"price": p, "size": q} for p, q in bids],
                        "asks": [{"price": p, "size": q} for p, q in asks],
                        "timestamp": data.get("lastUpdateId"),
                    })
                except Exception:
                    pass
        elif data.get("e") == "aggTrade":
            if self._sink is not None:
                try:
                    self._sink(now, {
                        "event_type": "last_trade_price",
                        "asset_id": data["s"],
                        "price": data["p"],
                        "size": data["q"],
                        # m=True: buyer was the maker, so the aggressor SOLD
                        "side": "SELL" if data.get("m") else "BUY",
                    })
                except Exception:
                    pass
