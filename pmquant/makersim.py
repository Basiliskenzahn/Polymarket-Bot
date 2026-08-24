"""Passive market-making backtest on the captured tick stream.

Simulates resting quotes joined to the best bid/ask of the replayed book,
with a deliberately conservative fill model: a quote fills only when a trade
prints STRICTLY THROUGH its price (queue position is never assumed), sized
min(quote size, trade size). Three variants isolate the flow-reversal effect:

- naive:  always quote both sides
- fade:   under strong trailing flow, quote only the side leaning AGAINST it
- chase:  quote only the side WITH the flow (control — should underperform
          fade if the measured overreaction/reversal is real)

Flow is computed from trades strictly before the current print (no
lookahead). Remaining inventory is marked at each token's final mid.
Assumes our size doesn't change market behavior; shorting a YES token
stands in for buying the NO book, which is equivalent at resolution.
"""
from __future__ import annotations

import bisect
import math
import sqlite3
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from .tickstudy import _Replay

QUOTE_SIZE = 20.0      # shares resting on each side
MAX_INV = 100.0        # per-token absolute inventory cap
FLOW_WINDOW = 60.0     # trailing seconds for the flow signal
FLOW_GATE = 0.4        # |flow| beyond this triggers one-sided quoting
MAX_SPREAD = 0.05      # don't quote into wide/stale books
DRIFT_H = 60.0         # post-fill horizon for adverse-selection measurement
GAP = 60.0


class _Book(dict):
    pass


class _MakerState:
    def __init__(self) -> None:
        self.inv: Dict[int, float] = defaultdict(float)   # asset -> shares
        self.cash_by: Dict[int, float] = defaultdict(float)
        self.cash = 0.0
        self.fills = 0
        self.shares = 0.0
        self.edge = 0.0                                   # $ vs mid at fill
        self.fill_log: List[Tuple[int, float, int, float]] = []
        # (asset, ts, +1 buy / -1 sell, shares)


def run_maker(ticks_path: str, db_path: str, venue: str = "polymarket") -> None:
    mconn = sqlite3.connect(db_path)
    yes_tokens = {row[0] for row in mconn.execute("SELECT token_yes FROM markets")}
    mconn.close()

    tconn = sqlite3.connect(ticks_path)
    id_to_token = dict(tconn.execute("SELECT id, token FROM assets"))
    keep = {aid for aid, tok in id_to_token.items() if tok in yes_tokens}

    replays: Dict[int, _Replay] = defaultdict(_Replay)
    makers = {name: _MakerState() for name in ("naive", "fade", "chase")}
    global_ts: List[float] = []
    n_trades = 0

    for ts, asset, kind, side, price, size, payload in tconn.execute(
            "SELECT ts, asset, kind, side, price, size, payload FROM ticks"
            " ORDER BY ts, rowid"):
        global_ts.append(ts)
        if asset not in keep:
            continue
        rep = replays[asset]
        if kind == "book":
            rep.set_book(ts, payload)
            continue
        if kind == "change":
            rep.change(ts, side, price, size)
            continue
        if kind != "trade" or side not in ("BUY", "SELL") or size <= 0:
            continue

        n_trades += 1
        if rep.bids and rep.asks:
            best_bid, best_ask = max(rep.bids), min(rep.asks)
            mid = (best_bid + best_ask) / 2
            spread = best_ask - best_bid
            flow = rep.flow(len(rep.trades), ts, FLOW_WINDOW)
            if venue == "crypto":
                # prices are in USD: gate spread at 5 bps, size quotes/caps in $
                quotable = 0 < spread <= mid * 5e-4
                q_size, inv_cap = 200.0 / mid, 1000.0 / mid
            else:
                quotable = 0 < spread <= MAX_SPREAD
                q_size, inv_cap = QUOTE_SIZE, MAX_INV
            if quotable:
                for name, mk in makers.items():
                    want_bid, want_ask = True, True
                    if flow is not None and abs(flow) > FLOW_GATE:
                        against_ask = flow > 0   # buy burst: fade = sell side
                        if name == "fade":
                            want_bid, want_ask = not against_ask, against_ask
                        elif name == "chase":
                            want_bid, want_ask = against_ask, not against_ask
                    inv = mk.inv[asset]
                    if inv >= inv_cap:
                        want_bid = False
                    if inv <= -inv_cap:
                        want_ask = False
                    filled = 0.0
                    if side == "SELL" and want_bid and price < best_bid:
                        filled = min(q_size, size)
                        mk.inv[asset] += filled
                        mk.cash -= filled * best_bid
                        mk.cash_by[asset] -= filled * best_bid
                        mk.edge += filled * (mid - best_bid)
                        mk.fill_log.append((asset, ts, +1, filled))
                    elif side == "BUY" and want_ask and price > best_ask:
                        filled = min(q_size, size)
                        mk.inv[asset] -= filled
                        mk.cash += filled * best_ask
                        mk.cash_by[asset] += filled * best_ask
                        mk.edge += filled * (best_ask - mid)
                        mk.fill_log.append((asset, ts, -1, filled))
                    if filled:
                        mk.fills += 1
                        mk.shares += filled
        rep.add_trade(ts, size if side == "BUY" else -size)
    tconn.close()

    def capture_alive(t: float) -> bool:
        i = bisect.bisect_right(global_ts, t)
        return 0 < i < len(global_ts) and t - global_ts[i - 1] <= GAP

    print(f"== maker backtest: {n_trades:,} trade prints, {len(replays)} YES"
          f" tokens, conservative strict-through fills ==")
    if venue == "crypto":
        print(f"quotes $200 notional joined at best, inventory cap ±$1000,"
              f" flow gate |{FLOW_GATE}| over {FLOW_WINDOW:.0f}s, spread ≤ 5 bps")
    else:
        print(f"quote {QUOTE_SIZE:.0f} sh joined at best, inventory cap"
              f" ±{MAX_INV:.0f} sh, flow gate |{FLOW_GATE}| over"
              f" {FLOW_WINDOW:.0f}s, spread ≤ {MAX_SPREAD}")
    for name, mk in makers.items():
        marked = mk.cash
        for asset, inv in mk.inv.items():
            if inv and replays[asset].mid_v:
                marked += inv * replays[asset].mid_v[-1]
        drift = d_n = 0.0
        for asset, ts, direction, filled in mk.fill_log:
            if not capture_alive(ts + DRIFT_H):
                continue
            m0 = replays[asset].mid_at(ts)
            m1 = replays[asset].mid_at(ts + DRIFT_H)
            if m0 is None or m1 is None:
                continue
            drift += direction * (m1 - m0) * filled
            d_n += 1
        gross_open = sum(abs(v) for v in mk.inv.values())
        print(f"-- {name} --")
        print(f"  fills: {mk.fills}  ({mk.shares:,.0f} sh)"
              f"  |  edge at fill: ${mk.edge:+.2f}"
              f"  |  60s post-fill drift: ${drift:+.2f} over {d_n:.0f} fills")
        print(f"  P&L marked to final mid: ${marked:+.2f}"
              f"  (open inventory {gross_open:,.0f} sh gross)")
        by_asset = []
        for asset in set(list(mk.cash_by) + list(mk.inv)):
            final_mid = replays[asset].mid_v[-1] if replays[asset].mid_v else 0.0
            by_asset.append((mk.cash_by[asset] + mk.inv[asset] * final_mid, asset))
        for pnl, asset in sorted(by_asset, key=lambda x: -abs(x[0]))[:3]:
            print(f"    {pnl:+8.2f}  inv {mk.inv[asset]:+6.0f} sh"
                  f"  token …{id_to_token[asset][-8:]}")
