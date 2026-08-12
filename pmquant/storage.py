"""SQLite persistence: tracked markets, book snapshots, trades, account state."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

from .models import Market, OrderBook

_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    id TEXT PRIMARY KEY,
    question TEXT, slug TEXT, condition_id TEXT, end_date TEXT,
    token_yes TEXT, token_no TEXT,
    outcome_yes TEXT, outcome_no TEXT,
    added_at REAL, resolved INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, token_id TEXT, market_id TEXT,
    best_bid REAL, best_ask REAL, mid REAL, spread REAL,
    bid_depth REAL, ask_depth REAL, imbalance REAL,
    top_levels TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_token_ts ON snapshots(token_id, ts);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, market_id TEXT, token_id TEXT,
    side TEXT, shares REAL, avg_price REAL, fee REAL,
    slippage REAL, signal TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS account (
    ts REAL, cash REAL, position_value REAL, equity REAL
);
CREATE TABLE IF NOT EXISTS positions (
    token_id TEXT PRIMARY KEY,
    market_id TEXT, shares REAL, cost REAL, entry_ts REAL, signal TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: str = "data/pmquant.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    # -- markets ------------------------------------------------------------

    def track_market(self, m: Market) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            (m.id, m.question, m.slug, m.condition_id, m.end_date,
             m.token_ids[0], m.token_ids[1], m.outcomes[0], m.outcomes[1],
             time.time()),
        )
        self.conn.commit()

    def tracked_markets(self, unresolved_only: bool = True) -> List[sqlite3.Row]:
        q = "SELECT * FROM markets"
        if unresolved_only:
            q += " WHERE resolved = 0"
        return self.conn.execute(q).fetchall()

    def mark_resolved(self, market_id: str) -> None:
        self.conn.execute("UPDATE markets SET resolved = 1 WHERE id = ?", (market_id,))
        self.conn.commit()

    # -- snapshots ----------------------------------------------------------

    def record_snapshot(self, market_id: str, book: OrderBook, depth_within: float) -> None:
        top = {
            "bids": [[l.price, l.size] for l in book.bids[:5]],
            "asks": [[l.price, l.size] for l in book.asks[:5]],
        }
        self.conn.execute(
            "INSERT INTO snapshots (ts, token_id, market_id, best_bid, best_ask, mid,"
            " spread, bid_depth, ask_depth, imbalance, top_levels)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (book.timestamp, book.token_id, market_id, book.best_bid, book.best_ask,
             book.mid, book.spread, book.depth("bid", depth_within),
             book.depth("ask", depth_within), book.imbalance(depth_within),
             json.dumps(top)),
        )

    def recent_mids(self, token_id: str, n: int = 20) -> List[float]:
        rows = self.conn.execute(
            "SELECT mid FROM snapshots WHERE token_id = ? AND mid IS NOT NULL"
            " ORDER BY ts DESC LIMIT ?", (token_id, n)).fetchall()
        return [r["mid"] for r in reversed(rows)]

    # -- trades / account ---------------------------------------------------

    def record_trade(self, market_id: str, token_id: str, side: str, shares: float,
                     avg_price: float, fee: float, slippage: float,
                     signal: str, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO trades (ts, market_id, token_id, side, shares, avg_price,"
            " fee, slippage, signal, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), market_id, token_id, side, shares, avg_price, fee,
             slippage, signal, note),
        )

    def record_account(self, cash: float, position_value: float) -> None:
        self.conn.execute("INSERT INTO account VALUES (?,?,?,?)",
                          (time.time(), cash, position_value, cash + position_value))

    def save_broker(self, cash: float, positions: Dict[str, Any]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO kv VALUES ('cash', ?)", (str(cash),))
        self.conn.execute("DELETE FROM positions")
        for p in positions.values():
            self.conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?)",
                              (p.token_id, p.market_id, p.shares, p.cost,
                               p.entry_ts, p.signal))
        self.conn.commit()

    def load_cash(self) -> Optional[float]:
        row = self.conn.execute("SELECT value FROM kv WHERE key = 'cash'").fetchone()
        return float(row["value"]) if row else None

    def load_positions(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM positions").fetchall()

    def commit(self) -> None:
        self.conn.commit()
