"""Real-time order books via Polymarket's CLOB websocket (market channel).

The websocket client is hand-rolled on the stdlib (socket/ssl/threading) —
just enough RFC 6455 for a JSON message stream: handshake, text frames with
client-side masking, ping/pong, fragmentation. MarketStream maintains live
books from `book` snapshots + `price_change` deltas; the engine samples them
and falls back to REST polling whenever a book is stale or the socket drops.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .models import Level, OrderBook

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Force a reconnect if the socket is open but silent this long. Quiet markets
# emit no book events, but our 10s app-level PINGs are answered with PONG
# messages, so a healthy connection never goes 45s without traffic.
STALE_AFTER = 45.0


class WebSocketClient:
    def __init__(self, url: str, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._buf = b""

    def connect(self) -> None:
        u = urlparse(self.url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=self.timeout)
        if u.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(
                raw, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        raw.sendall((
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {u.hostname}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = raw.recv(4096)
            if not chunk:
                raise ConnectionError("handshake: connection closed")
            resp += chunk
        head, rest = resp.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"handshake rejected: {head[:100]!r}")
        self._buf = rest  # frames may already trail the response headers
        self.sock = raw

    def close(self) -> None:
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, data: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) +
                          bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send_text(self, payload: str) -> None:
        self._send_frame(0x1, payload.encode())

    def send_pong(self, payload: bytes = b"") -> None:
        """Unsolicited pong — some venues (Binance) accept this as keepalive."""
        self._send_frame(0xA, payload)

    def recv_message(self) -> Optional[str]:
        """Next text message; answers pings transparently, None on close.

        On socket timeout, bytes consumed mid-frame are restored to the
        buffer so the parser never desyncs.
        """
        consumed = bytearray()

        def read(n: int) -> bytes:
            data = self._read(n)
            consumed.extend(data)
            return data

        fragments: List[bytes] = []
        try:
            while True:
                b1, b2 = read(2)
                fin, opcode = b1 & 0x80, b1 & 0x0F
                length = b2 & 0x7F
                if length == 126:
                    length = struct.unpack(">H", read(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", read(8))[0]
                mask = read(4) if b2 & 0x80 else b""
                payload = read(length)
                if mask:
                    payload = bytes(b ^ mask[i % 4]
                                    for i, b in enumerate(payload))
                if opcode == 0x8:    # close
                    return None
                if opcode == 0x9:    # ping -> pong
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 0xA:    # pong
                    continue
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", "replace")
        except socket.timeout:
            self._buf = bytes(consumed) + self._buf
            raise


class MarketStream:
    """Live order books for a set of outcome tokens, updated on a reader thread."""

    def __init__(self, asset_ids: List[str], url: str = WS_URL, on_event=None):
        self._assets = list(dict.fromkeys(asset_ids))
        self.url = url
        self._sink = on_event  # called as on_event(recv_ts, event_dict), ws thread
        self.connected = False
        self._last_event = 0.0
        self._lock = threading.Lock()
        # token -> {"bids": {price: size}, "asks": {...}, "tick": float, "ts": float}
        self._books: Dict[str, dict] = {}
        self._stop = threading.Event()
        self._resubscribe = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_assets(self, asset_ids: List[str]) -> None:
        asset_ids = list(dict.fromkeys(asset_ids))
        if set(asset_ids) != set(self._assets):
            self._assets = asset_ids
            self._resubscribe.set()  # market channel needs a fresh subscribe

    def data_age(self) -> float:
        """Seconds since the last message from the socket (inf if never).

        `connected` only says the last handshake succeeded; a half-dead TCP
        connection (laptop sleep, NAT drop) keeps it True while nothing
        arrives. Freshness is the honest health signal.
        """
        last = self._last_event
        return time.time() - last if last else float("inf")

    def book(self, token_id: str, max_age: float = 90.0) -> Optional[OrderBook]:
        """Current book sampled now, or None if we have no fresh state."""
        with self._lock:
            state = self._books.get(token_id)
            if state is None or time.time() - state["ts"] > max_age:
                return None
            bids = sorted((Level(p, s) for p, s in state["bids"].items()),
                          key=lambda l: -l.price)
            asks = sorted((Level(p, s) for p, s in state["asks"].items()),
                          key=lambda l: l.price)
            tick = state["tick"]
        if not bids and not asks:
            return None
        return OrderBook(token_id=token_id, bids=bids, asks=asks, tick_size=tick)

    # -- reader thread ---------------------------------------------------------

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            ws = WebSocketClient(self.url)
            try:
                ws.connect()
                ws.send_text(json.dumps({"assets_ids": self._assets,
                                         "type": "market"}))
                self.connected = True
                backoff = 1.0
                self._resubscribe.clear()
                last_ping = time.time()
                self._last_event = time.time()
                while not self._stop.is_set() and not self._resubscribe.is_set():
                    if time.time() - last_ping > 10:
                        ws.send_text("PING")  # app-level keepalive
                        last_ping = time.time()
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
            if not self._stop.is_set() and not self._resubscribe.is_set():
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def _handle(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except ValueError:
            return  # "PONG" and other non-JSON keepalives
        events = data if isinstance(data, list) else [data]
        now = time.time()
        if self._sink is not None:  # outside the lock: sink I/O must not block book()
            for ev in events:
                if isinstance(ev, dict) and ev.get("asset_id"):
                    try:
                        self._sink(now, ev)
                    except Exception:
                        pass  # capture must never take down the live feed
        with self._lock:
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                asset = ev.get("asset_id")
                if not asset:
                    continue
                kind = ev.get("event_type")
                if kind == "book":
                    bids = ev.get("bids") or ev.get("buys") or []
                    asks = ev.get("asks") or ev.get("sells") or []
                    self._books[asset] = {
                        "bids": {float(l["price"]): float(l["size"])
                                 for l in bids if float(l["size"]) > 0},
                        "asks": {float(l["price"]): float(l["size"])
                                 for l in asks if float(l["size"]) > 0},
                        "tick": float(ev.get("tick_size") or 0.01),
                        "ts": now,
                    }
                elif kind == "price_change":
                    state = self._books.get(asset)
                    if state is None:
                        continue
                    for change in ev.get("changes") or [ev]:
                        try:
                            price = float(change["price"])
                            size = float(change["size"])
                            side = str(change.get("side", "")).upper()
                        except (KeyError, TypeError, ValueError):
                            continue
                        levels = state["bids"] if side == "BUY" else state["asks"]
                        if size <= 0:
                            levels.pop(price, None)
                        else:
                            levels[price] = size
                    state["ts"] = now
                elif kind == "tick_size_change":
                    state = self._books.get(asset)
                    if state is not None:
                        state["tick"] = float(ev.get("new_tick_size")
                                              or state["tick"])
                        state["ts"] = now
