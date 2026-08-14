"""Crypto collector: capture Binance books + trades into the pmquant stores.

Collection only, no paper trading — the point is tick data for the flow and
maker studies (`--venue crypto flow` / `maker`), which read the same schemas
the Polymarket pipeline writes.
"""
from __future__ import annotations

import time
from typing import List, Optional

from .binance import BinanceStream, top_symbols
from .storage import Store
from .ticks import TickStore


def run_crypto(store: Store, ticks_path: str, top: int = 5,
               interval: float = 30.0, duration: Optional[float] = None,
               symbols: Optional[List[str]] = None) -> None:
    if not symbols:
        symbols = top_symbols(top)
    for sym in symbols:
        store.track_symbol(sym)
    ticks = TickStore(ticks_path)
    stream = BinanceStream(symbols, on_event=ticks.ingest)
    stream.start()
    print(f"collecting {len(symbols)} symbols: {', '.join(symbols)}", flush=True)

    started = time.time()
    while duration is None or time.time() - started < duration:
        tick_start = time.time()
        mids = []
        for sym in symbols:
            book = stream.book(sym)
            if book is None:
                continue
            # depth band for imbalance: 10 bps around the touch
            within = (book.mid or 1.0) * 0.001
            store.record_snapshot(sym, book, within)
            if book.mid is not None:
                mids.append(f"{sym} {book.mid:,.2f}")
        store.commit()
        state = "live" if stream.connected else "reconnecting"
        print(f"{time.strftime('%H:%M:%S')} ws {state} | ticks {ticks.total:,}"
              f" | {' | '.join(mids[:4])}", flush=True)
        time.sleep(max(0.0, interval - (time.time() - tick_start)))
