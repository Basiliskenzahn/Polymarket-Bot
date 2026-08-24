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


class _Acc:
    """Streaming sums for Pearson r / t-stat / hit rate (no sample storage)."""
    __slots__ = ("n", "sx", "sy", "sxx", "syy", "sxy", "moved", "hits")

    def __init__(self) -> None:
        self.n = 0
        self.sx = self.sy = self.sxx = self.syy = self.sxy = 0.0
        self.moved = self.hits = 0

    def add(self, x: float, y: float) -> None:
        self.n += 1
        self.sx += x; self.sy += y
        self.sxx += x * x; self.syy += y * y; self.sxy += x * y
        if y != 0:
            self.moved += 1
            if x * y > 0:
                self.hits += 1

    def pearson(self) -> Optional[float]:
        n = self.n
        vx = self.sxx - self.sx * self.sx / n
        vy = self.syy - self.sy * self.sy / n
        if vx <= 0 or vy <= 0:
            return None
        return (self.sxy - self.sx * self.sy / n) / math.sqrt(vx * vy)


class _Replay:
    """One token's book state + mid history + trade tape."""

    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.mid_ts: List[float] = []
        self.mid_v: List[float] = []
        self.trades: List[Tuple[float, float]] = []  # (ts, signed size)
        # prefix sums over the tape for O(log n) trailing-window flow
        self.tr_ts: List[float] = []
        self.cum_buy: List[float] = [0.0]
        self.cum_sell: List[float] = [0.0]

    def add_trade(self, ts: float, signed: float) -> None:
        self.trades.append((ts, signed))
        self.tr_ts.append(ts)
        self.cum_buy.append(self.cum_buy[-1] + max(signed, 0.0))
        self.cum_sell.append(self.cum_sell[-1] + max(-signed, 0.0))

    def flow(self, k: int, now: float, window: float) -> Optional[float]:
        """Signed flow ratio over tape indices [i, k) with ts > now - window."""
        i = bisect.bisect_right(self.tr_ts, now - window, 0, k)
        buy = self.cum_buy[k] - self.cum_buy[i]
        sell = self.cum_sell[k] - self.cum_sell[i]
        if buy + sell <= 0:
            return None
        return (buy - sell) / (buy + sell)

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


def run_study(ticks_path: str, db_path: str, venue: str = "polymarket") -> None:
    """venue='crypto': forward move is a relative return (bps) so that assets
    with very different price levels (BTC vs. a $0.10 alt) pool sensibly, and
    a per-asset breakdown is printed."""
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
            rep.add_trade(ts, size if side == "BUY" else -size)
    tconn.close()

    def capture_alive(t: float) -> bool:
        i = bisect.bisect_right(global_ts, t)
        return 0 < i < len(global_ts) and t - global_ts[i - 1] <= GAP

    relative = venue == "crypto"
    pooled: Dict[Tuple[float, float], _Acc] = defaultdict(_Acc)
    by_asset: Dict[Tuple[int, float, float], _Acc] = defaultdict(_Acc)
    n_trades = sum(len(r.trades) for r in replays.values())
    for asset, rep in replays.items():
        tape = rep.trades
        for k, (t, _signed) in enumerate(tape):
            mid_now = rep.mid_at(t)
            if mid_now is None or mid_now <= 0:
                continue
            feats = {}
            for w in WINDOWS:
                f = rep.flow(k + 1, t, w)   # inclusive of the current print
                if f is not None:
                    feats[w] = f
            if not feats:
                continue
            for h in HORIZONS:
                if not capture_alive(t + h):
                    continue
                mid_fwd = rep.mid_at(t + h)
                if mid_fwd is None:
                    continue
                dmid = mid_fwd - mid_now
                if relative:
                    dmid = dmid / mid_now * 1e4   # bps
                for w, f in feats.items():
                    pooled[(w, h)].add(f, dmid)
                    by_asset[(asset, w, h)].add(f, dmid)

    unit = "bps return" if relative else "mid move"
    print(f"== trade flow -> forward {unit} "
          f"(replayed {n_events:,} events, {n_trades:,} trade prints, "
          f"{len(replays)} tokens) ==")
    print("flow = (taker buys - taker sells) / total, trailing window;"
          " sampled at each trade print")

    def line(acc: _Acc, label: str) -> str:
        n = acc.n
        if n < 50:
            return f"  {label}: n={n} (insufficient)"
        r = acc.pearson()
        if r is None:
            return f"  {label}: degenerate"
        t_stat = r * math.sqrt((n - 2) / max(1e-12, 1 - r * r))
        hit = acc.hits / acc.moved if acc.moved else float("nan")
        mark = " ***" if abs(t_stat) > 2 else ""
        return (f"  {label}: n={n:<8} r={r:+.3f}  t={t_stat:+7.2f}  "
                f"hit={hit:.1%} of {acc.moved} moved{mark}")

    for w in WINDOWS:
        print(f"-- {w:.0f}s flow window --")
        for h in HORIZONS:
            print(line(pooled[(w, h)], f"{h:>4.0f}s ahead"))
    if relative:
        print("-- per asset (r at each horizon; *** = |t|>2) --")
        for asset in sorted(replays, key=lambda a: id_to_token[a]):
            name = id_to_token[asset]
            for w in WINDOWS:
                cells = []
                for h in HORIZONS:
                    acc = by_asset[(asset, w, h)]
                    r = acc.pearson() if acc.n >= 50 else None
                    if r is None:
                        cells.append(f"{h:>3.0f}s     n/a ")
                        continue
                    t_stat = r * math.sqrt((acc.n - 2) / max(1e-12, 1 - r * r))
                    cells.append(f"{h:>3.0f}s {r:+.3f}{'*' if abs(t_stat) > 2 else ' '}")
                print(f"  {name:<10} {w:>3.0f}s: " + "  ".join(cells)
                      + f"   n={by_asset[(asset, w, HORIZONS[0])].n:,}")
    print("note: t-stats assume independent samples; overlapping windows"
          " inflate them — read r and hit rate, not t.")
