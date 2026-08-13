"""Trade-flow study on the captured tick stream.

Replays each YES token's order book from checkpoints + delta rows, then asks:
does signed trade flow (taker buys minus taker sells over a trailing window)
predict the mid-price move over the next 1-60 seconds?

Sampling is event-driven — features are evaluated at each trade print — so
results are conditional on "a trade just happened", which is how flow signals
are used in practice. Forward returns are dropped when the capture stream had
a gap (outage, laptop asleep) inside the horizon.
"""
from __future__ import annotations

import bisect
import json
import math
import sqlite3
import zlib
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from .analysis import _pearson

WINDOWS = (10.0, 60.0)      # trailing seconds of flow to aggregate
HORIZONS = (1.0, 5.0, 15.0, 60.0)
GAP = 60.0                  # capture silence longer than this = data gap


class _Replay:
    """One token's book state + mid history + trade tape."""

    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.mid_ts: List[float] = []
        self.mid_v: List[float] = []
        self.trades: List[Tuple[float, float]] = []  # (ts, signed size)

    def set_book(self, ts: float, payload: bytes) -> None:
        data = json.loads(zlib.decompress(payload))
        self.bids = {float(l["price"]): float(l["size"]) for l in data["bids"] or []}
        self.asks = {float(l["price"]): float(l["size"]) for l in data["asks"] or []}
        self._mark(ts)

    def change(self, ts: float, side: Optional[str], price: float,
               size: float) -> None:
        levels = self.bids if side == "BUY" else self.asks
        if size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        self._mark(ts)

    def _mark(self, ts: float) -> None:
        if not self.bids or not self.asks:
            return
        mid = (max(self.bids) + min(self.asks)) / 2
        if not self.mid_v or self.mid_v[-1] != mid:
            self.mid_ts.append(ts)
            self.mid_v.append(mid)

    def mid_at(self, ts: float) -> Optional[float]:
        i = bisect.bisect_right(self.mid_ts, ts)
        return self.mid_v[i - 1] if i else None


def run_study(ticks_path: str, db_path: str) -> None:
    mconn = sqlite3.connect(db_path)
    yes_tokens = {row[0] for row in mconn.execute("SELECT token_yes FROM markets")}
    mconn.close()

    tconn = sqlite3.connect(ticks_path)
    id_to_token = dict(tconn.execute("SELECT id, token FROM assets"))
    keep = {aid for aid, tok in id_to_token.items() if tok in yes_tokens}

    replays: Dict[int, _Replay] = defaultdict(_Replay)
    global_ts: List[float] = []
    n_events = 0
    for ts, asset, kind, side, price, size, payload in tconn.execute(
            "SELECT ts, asset, kind, side, price, size, payload FROM ticks"
            " ORDER BY ts"):
        n_events += 1
        global_ts.append(ts)
        if asset not in keep:
            continue
        rep = replays[asset]
        if kind == "book":
            rep.set_book(ts, payload)
        elif kind == "change":
            rep.change(ts, side, price, size)
        elif kind == "trade" and side in ("BUY", "SELL") and size > 0:
            rep.trades.append((ts, size if side == "BUY" else -size))
    tconn.close()

    def capture_alive(t: float) -> bool:
        i = bisect.bisect_right(global_ts, t)
        return 0 < i < len(global_ts) and t - global_ts[i - 1] <= GAP

    samples: Dict[Tuple[float, float], List[Tuple[float, float]]] = defaultdict(list)
    n_trades = sum(len(r.trades) for r in replays.values())
    for rep in replays.values():
        tape = rep.trades
        for k, (t, _signed) in enumerate(tape):
            mid_now = rep.mid_at(t)
            if mid_now is None:
                continue
            feats = {}
            for w in WINDOWS:
                j = k
                buy = sell = 0.0
                while j >= 0 and tape[j][0] > t - w:
                    s = tape[j][1]
                    buy, sell = buy + max(s, 0), sell + max(-s, 0)
                    j -= 1
                if buy + sell > 0:
                    feats[w] = (buy - sell) / (buy + sell)
            for h in HORIZONS:
                if not capture_alive(t + h):
                    continue
                mid_fwd = rep.mid_at(t + h)
                if mid_fwd is None:
                    continue
                dmid = mid_fwd - mid_now
                for w, f in feats.items():
                    samples[(w, h)].append((f, dmid))

    print(f"== trade flow -> forward mid move "
          f"(replayed {n_events:,} events, {n_trades:,} trade prints, "
          f"{len(replays)} YES tokens) ==")
    print("flow = (taker buys - taker sells) / total, trailing window;"
          " sampled at each trade print")
    for w in WINDOWS:
        print(f"-- {w:.0f}s flow window --")
        for h in HORIZONS:
            pairs = samples.get((w, h), [])
            n = len(pairs)
            if n < 50:
                print(f"  {h:>4.0f}s ahead: n={n} (insufficient)")
                continue
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            r = _pearson(xs, ys)
            if r is None:
                print(f"  {h:>4.0f}s ahead: degenerate")
                continue
            t_stat = r * math.sqrt((n - 2) / max(1e-12, 1 - r * r))
            moved = [(x, y) for x, y in pairs if y != 0]
            hit = (sum(1 for x, y in moved if x * y > 0) / len(moved)
                   if moved else float("nan"))
            mark = " ***" if abs(t_stat) > 2 else ""
            print(f"  {h:>4.0f}s ahead: n={n:<6} r={r:+.3f}  t={t_stat:+6.2f}  "
                  f"hit={hit:.1%} of {len(moved)} moved{mark}")
