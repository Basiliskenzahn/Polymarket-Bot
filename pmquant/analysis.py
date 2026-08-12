"""Signal evaluation: does order-book imbalance predict forward mid-price moves?

Pools (imbalance, forward mid-price change) pairs across all tracked markets
(YES tokens only — the NO book mirrors it) and reports, per horizon: Pearson
correlation with t-statistic, directional hit rate, and mean forward move in
the bottom vs top imbalance tercile. Mid changes are in probability points,
so pooling across binary markets is scale-consistent.
"""
from __future__ import annotations

import math
import sqlite3
from typing import List, Optional, Tuple

Pair = Tuple[float, float]  # (imbalance now, mid change over horizon)


def _forward_pairs(rows: List[Tuple[float, Optional[float], Optional[float]]],
                   horizon: float, tolerance: float = 0.5) -> List[Pair]:
    """Match each snapshot with the first one >= horizon seconds later.

    Pairs are dropped when the follow-up snapshot arrives more than
    horizon * tolerance late (collection gap), so sleep/downtime gaps
    don't pollute the sample.
    """
    pairs: List[Pair] = []
    j, n = 0, len(rows)
    for i in range(n):
        ts0, mid0, imb0 = rows[i]
        if mid0 is None or imb0 is None:
            continue
        target = ts0 + horizon
        while j < n and rows[j][0] < target:
            j += 1
        if j >= n:
            break
        ts1, mid1, _ = rows[j]
        if mid1 is None or ts1 - target > horizon * tolerance:
            continue
        pairs.append((imb0, mid1 - mid0))
    return pairs


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return cov / math.sqrt(vx * vy)


def _report(horizon: float, pairs: List[Pair]) -> None:
    n = len(pairs)
    if n < 30:
        print(f"{horizon:>6.0f}s  n={n}: insufficient data "
              f"(keep the collector running)")
        return
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    r = _pearson(xs, ys)
    if r is None:
        print(f"{horizon:>6.0f}s  n={n}: degenerate sample (no variance)")
        return
    t = r * math.sqrt((n - 2) / max(1e-12, 1 - r * r))
    moved = [(x, y) for x, y in pairs if y != 0]
    hit = (sum(1 for x, y in moved if x * y > 0) / len(moved)) if moved else float("nan")
    ranked = sorted(pairs)
    third = n // 3
    low = sum(y for _, y in ranked[:third]) / third
    high = sum(y for _, y in ranked[-third:]) / third
    print(f"{horizon:>6.0f}s  n={n:<7} r={r:+.3f}  t={t:+.2f}  "
          f"hit={hit:.1%} ({len(moved)} moved)  "
          f"E[Δmid|low imb]={low:+.4f}  E[Δmid|high imb]={high:+.4f}")


def evaluate(conn: sqlite3.Connection, horizons: List[float]) -> None:
    tokens = [row[0] for row in conn.execute("SELECT token_yes FROM markets")]
    print("== imbalance -> forward mid-price move ==")
    print("(r > 0 with |t| > 2 would mean imbalance carries predictive signal)")
    for horizon in horizons:
        pairs: List[Pair] = []
        for token in tokens:
            rows = conn.execute(
                "SELECT ts, mid, imbalance FROM snapshots"
                " WHERE token_id = ? ORDER BY ts", (token,)).fetchall()
            pairs += _forward_pairs(
                [(row[0], row[1], row[2]) for row in rows], horizon)
        _report(horizon, pairs)
