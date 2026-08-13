"""Tick-level capture: every websocket event persisted for research.

Polymarket's market channel emits full order-book snapshots rather than
per-level deltas, so storing raw events verbatim costs ~1.5 GB/day. Instead
this store delta-encodes the stream itself:

- kind='book'      zlib-compressed full snapshot (BLOB payload) — written on
                   first sight of a token and as a checkpoint every
                   `checkpoint_secs`, bounding replay length.
- kind='change'    one flat row per changed price level (size=0 -> removed),
                   synthesized by diffing consecutive snapshots; native
                   `price_change` events are recorded the same way.
- kind='trade'     last-trade prints (price, size, aggressor side).
- kind='tick_size' tick size changes.

The book at any time t = latest 'book' row <= t, then apply 'change' rows in
order. Written from the websocket reader thread with batched commits; the
connection is created lazily so it lives on that thread.
"""
from __future__ import annotations

import json
import sqlite3
import time
import zlib
from typing import Any, Dict, List, Optional, Tuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY,
    token TEXT UNIQUE   -- Polymarket outcome token id (~77-digit string)
);
CREATE TABLE IF NOT EXISTS ticks (
    ts REAL,        -- local receive time (epoch seconds)
    asset INTEGER,  -- assets.id (token ids are interned: ~77 bytes -> ~1)
    kind TEXT,      -- book | change | trade | tick_size
    side TEXT,      -- BUY / SELL for change & trade rows
    price REAL,
    size REAL,      -- for 'change': new size at level, 0 = level removed
    payload BLOB    -- zlib(full book JSON) for kind='book' only
);
CREATE INDEX IF NOT EXISTS idx_ticks_asset_ts ON ticks(asset, ts);
"""

Row = Tuple[float, int, str, Optional[str], Optional[float], Optional[float],
            Optional[bytes]]
Side = Dict[float, float]  # price -> size


def _parse_side(levels: Any) -> Side:
    return {float(l["price"]): float(l["size"]) for l in (levels or [])}


class TickStore:
    def __init__(self, path: str, batch: int = 200, flush_secs: float = 2.0,
                 checkpoint_secs: float = 600.0):
        self.path = path
        self.batch = batch
        self.flush_secs = flush_secs
        self.checkpoint_secs = checkpoint_secs
        self.total = 0
        self._conn: Optional[sqlite3.Connection] = None
        self._pending: List[Row] = []
        self._last_flush = time.time()
        self._state: Dict[str, Dict[str, Side]] = {}   # token -> current book
        self._last_checkpoint: Dict[str, float] = {}
        self._asset_ids: Dict[str, int] = {}           # token -> assets.id

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
        return self._conn

    def _asset_id(self, token: str) -> int:
        cached = self._asset_ids.get(token)
        if cached is None:
            conn = self._connection()
            conn.execute("INSERT OR IGNORE INTO assets (token) VALUES (?)", (token,))
            cached = conn.execute("SELECT id FROM assets WHERE token = ?",
                                  (token,)).fetchone()[0]
            self._asset_ids[token] = cached
        return cached

    def ingest(self, ts: float, event: Dict[str, Any]) -> None:
        token = str(event.get("asset_id"))
        asset = self._asset_id(token)
        kind = event.get("event_type")
        rows: List[Row] = []
        if kind == "book":
            rows = self._ingest_book(ts, token, asset, event)
        elif kind == "price_change":
            state = self._state.get(token)
            for change in event.get("changes") or [event]:
                try:
                    price = float(change["price"])
                    size = float(change["size"])
                    side = str(change.get("side", "")).upper()
                except (KeyError, TypeError, ValueError):
                    continue
                rows.append((ts, asset, "change", side or None, price, size, None))
                if state is not None and side in ("BUY", "SELL"):
                    levels = state["bids"] if side == "BUY" else state["asks"]
                    if size <= 0:
                        levels.pop(price, None)
                    else:
                        levels[price] = size
        elif kind == "last_trade_price":
            try:
                rows.append((ts, asset, "trade",
                             str(event.get("side", "")).upper() or None,
                             float(event["price"]),
                             float(event.get("size") or 0), None))
            except (KeyError, TypeError, ValueError):
                return
        elif kind == "tick_size_change":
            rows.append((ts, asset, "tick_size", None,
                         float(event.get("new_tick_size") or 0), None, None))
        else:
            return
        if rows:
            self._pending.extend(rows)
            self.total += len(rows)
        if len(self._pending) >= self.batch or ts - self._last_flush > self.flush_secs:
            self.flush()

    def _ingest_book(self, ts: float, token: str, asset: int,
                     event: Dict[str, Any]) -> List[Row]:
        bids = _parse_side(event.get("bids") or event.get("buys"))
        asks = _parse_side(event.get("asks") or event.get("sells"))
        rows: List[Row] = []
        prev = self._state.get(token)
        if prev is not None:  # delta-encode against the previous snapshot
            for side_name, new, old in (("BUY", bids, prev["bids"]),
                                        ("SELL", asks, prev["asks"])):
                for price in old.keys() - new.keys():
                    rows.append((ts, asset, "change", side_name, price, 0.0, None))
                for price, size in new.items():
                    if old.get(price) != size:
                        rows.append((ts, asset, "change", side_name, price, size,
                                     None))
        if prev is None or ts - self._last_checkpoint.get(token, 0) > self.checkpoint_secs:
            payload = zlib.compress(json.dumps({
                "bids": event.get("bids") or event.get("buys") or [],
                "asks": event.get("asks") or event.get("sells") or [],
                "server_ts": event.get("timestamp"),
            }).encode(), 6)
            rows.append((ts, asset, "book", None, None, None, payload))
            self._last_checkpoint[token] = ts
        self._state[token] = {"bids": bids, "asks": asks}
        return rows

    def flush(self) -> None:
        if self._pending:
            conn = self._connection()
            conn.executemany("INSERT INTO ticks VALUES (?,?,?,?,?,?,?)",
                             self._pending)
            conn.commit()
            self._pending = []
        self._last_flush = time.time()
