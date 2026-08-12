"""Trading engine: scan -> collect books -> evaluate signals -> paper trade.

Strategy (deliberately simple, transparent baselines):
- dutch_book: buy YES+NO when their combined ask < $1 (risk-free, hold to resolution)
- imbalance:  buy the side the book is leaning toward; exit on signal flip or timeout.
  Prediction markets have no shorting, so "sell YES" is expressed as "buy NO".
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional

from .api import ClobClient, GammaClient
from .models import Market, OrderBook
from .scanner import scan
from .signals import dutch_book, imbalance_score, momentum_score
from .simulator import InsufficientLiquidity, PaperBroker, Position
from .storage import Store


@dataclass
class Config:
    top_markets: int = 15
    min_liquidity: float = 10_000
    min_volume24h: float = 5_000
    depth_within: float = 0.05      # band around the touch for depth/imbalance
    enter_threshold: float = 0.4    # |imbalance| needed to open a position
    exit_threshold: float = 0.1     # flip below this closes it
    max_spread: float = 0.03        # don't take positions in wide books
    max_hold_secs: float = 3600.0
    order_usdc: float = 25.0        # target clip size per entry
    max_positions: int = 10
    arb_min_edge: float = 0.005
    arb_max_usdc: float = 100.0


class Engine:
    def __init__(self, store: Store, broker: PaperBroker, cfg: Config = Config(),
                 use_ws: bool = True):
        self.store = store
        self.broker = broker
        self.cfg = cfg
        self.gamma = GammaClient()
        self.clob = ClobClient()
        self.use_ws = use_ws
        self.stream = None          # set in run() once the universe is known
        self._ws_books = 0          # books served from the websocket...
        self._rest_books = 0        # ...vs REST fallback
        self._restore_positions()

    def _restore_positions(self) -> None:
        saved_cash = self.store.load_cash()
        if saved_cash is not None:
            self.broker.cash = saved_cash
        for row in self.store.load_positions():
            self.broker.positions[row["token_id"]] = Position(
                token_id=row["token_id"], market_id=row["market_id"],
                shares=row["shares"], cost=row["cost"],
                entry_ts=row["entry_ts"], signal=row["signal"])

    # -- universe -------------------------------------------------------------

    def refresh_universe(self) -> int:
        markets = scan(self.gamma, self.cfg.top_markets,
                       self.cfg.min_liquidity, self.cfg.min_volume24h)
        for m in markets:
            self.store.track_market(m)
        return len(markets)

    # -- one tick over all tracked markets -------------------------------------

    def tick(self) -> None:
        mids: Dict[str, float] = {}
        for row in self.store.tracked_markets():
            try:
                books = self._fetch_books(row["token_yes"], row["token_no"])
            except Exception as exc:  # network hiccup: skip market this tick
                print(f"  ! {row['question'][:50]}: {exc}")
                continue
            if books is None:
                continue
            book_yes, book_no = books
            self.store.record_snapshot(row["id"], book_yes, self.cfg.depth_within)
            self.store.record_snapshot(row["id"], book_no, self.cfg.depth_within)
            for b in (book_yes, book_no):
                if b.mid is not None:
                    mids[b.token_id] = b.mid
            self._trade_arb(row, book_yes, book_no)
            self._trade_imbalance(row, book_yes, book_no)
        self.store.record_account(self.broker.cash,
                                  self.broker.position_value(mids))
        self.store.save_broker(self.broker.cash, self.broker.positions)
        self.store.commit()

    def _fetch_books(self, token_yes: str, token_no: str):
        if self.stream is not None:
            book_yes = self.stream.book(token_yes)
            book_no = self.stream.book(token_no)
            if book_yes is not None and book_no is not None:
                self._ws_books += 2
                return book_yes, book_no
        self._rest_books += 2
        payloads = self.clob.books([token_yes, token_no])
        by_id = {p["asset_id"]: OrderBook.from_api(p) for p in payloads}
        if token_yes not in by_id or token_no not in by_id:
            return None
        return by_id[token_yes], by_id[token_no]

    # -- strategies -------------------------------------------------------------

    def _trade_arb(self, row, book_yes: OrderBook, book_no: OrderBook) -> None:
        opp = dutch_book(book_yes, book_no, min_edge=self.cfg.arb_min_edge)
        if opp is None:
            return
        pair_cost = opp.ask_yes + opp.ask_no
        shares = min(opp.max_shares, self.cfg.arb_max_usdc / pair_cost,
                     self.broker.cash / pair_cost * 0.5)
        if shares < 1:
            return
        try:
            fy = self.broker.buy(book_yes, row["id"], shares, signal="dutch_book")
            fn = self.broker.buy(book_no, row["id"], shares, signal="dutch_book")
        except InsufficientLiquidity:
            return
        for token, fill in ((row["token_yes"], fy), (row["token_no"], fn)):
            self.store.record_trade(row["id"], token, "buy", fill.shares,
                                    fill.avg_price, fill.fee, fill.slippage,
                                    "dutch_book", f"edge={opp.edge:.4f}")
        print(f"  ARB  {row['question'][:50]}  edge={opp.edge:.3f} x{shares:.0f}")

    def _trade_imbalance(self, row, book_yes: OrderBook, book_no: OrderBook) -> None:
        score = imbalance_score(book_yes, self.cfg.depth_within)
        if score is None:
            return
        mids = self.store.recent_mids(row["token_yes"])
        mom = momentum_score(mids)
        # exits first: close directional positions whose signal has flipped/expired
        for token, book, direction in ((row["token_yes"], book_yes, 1),
                                       (row["token_no"], book_no, -1)):
            pos = self.broker.position(token)
            if pos is None or pos.signal != "imbalance":
                continue
            flipped = score * direction < self.cfg.exit_threshold
            expired = time.time() - pos.entry_ts > self.cfg.max_hold_secs
            if flipped or expired:
                try:
                    fill = self.broker.sell(book)
                except InsufficientLiquidity:
                    continue
                self.store.record_trade(row["id"], token, "sell", fill.shares,
                                        fill.avg_price, fill.fee, fill.slippage,
                                        "imbalance", "flip" if flipped else "timeout")
                print(f"  EXIT {row['question'][:50]}  @{fill.avg_price:.3f}")
        # entries: strong imbalance confirmed by momentum, tight spread only
        if len(self.broker.positions) >= self.cfg.max_positions:
            return
        if abs(score) < self.cfg.enter_threshold:
            return
        if mom is not None and mom * score < 0:  # momentum disagrees: stand down
            return
        token, book = ((row["token_yes"], book_yes) if score > 0
                       else (row["token_no"], book_no))
        if self.broker.position(token) is not None:
            return
        if book.spread is None or book.spread > self.cfg.max_spread:
            return
        if not book.asks:
            return
        shares = min(self.cfg.order_usdc / book.asks[0].price,
                     book.depth("ask", self.cfg.depth_within))
        if shares < 1:
            return
        try:
            fill = self.broker.buy(book, row["id"], shares, signal="imbalance")
        except InsufficientLiquidity:
            return
        self.store.record_trade(row["id"], token, "buy", fill.shares,
                                fill.avg_price, fill.fee, fill.slippage,
                                "imbalance", f"score={score:.2f}")
        print(f"  OPEN {row['question'][:50]}  imb={score:+.2f} @{fill.avg_price:.3f}")

    # -- resolution -------------------------------------------------------------

    def settle_resolved(self) -> None:
        held_markets = {p.market_id for p in self.broker.positions.values()}
        for row in self.store.tracked_markets():
            if row["id"] not in held_markets:
                continue
            try:
                m = Market.from_gamma(self.gamma.market(row["id"]))
            except Exception:
                continue
            if m is None or not m.closed or len(m.outcome_prices) != 2:
                continue
            for token, payout in zip(m.token_ids, m.outcome_prices):
                pnl = self.broker.settle(token, payout)
                if pnl:
                    self.store.record_trade(m.id, token, "settle", 0, payout, 0, 0,
                                            "resolution", f"pnl={pnl:.2f}")
            self.store.mark_resolved(m.id)
            print(f"  SETTLED {m.question[:50]}")

    # -- main loop --------------------------------------------------------------

    def _tracked_tokens(self):
        tokens = []
        for row in self.store.tracked_markets():
            tokens += [row["token_yes"], row["token_no"]]
        return tokens

    def run(self, interval: float = 30.0, duration: Optional[float] = None,
            rescan_every: float = 1800.0) -> None:
        started = time.time()
        n = self.refresh_universe()
        if self.use_ws:
            from .ws import MarketStream
            self.stream = MarketStream(self._tracked_tokens())
            self.stream.start()
        feed = "websocket (REST fallback)" if self.stream else "REST polling"
        print(f"tracking {n} markets via {feed} | paper cash ${self.broker.cash:.2f}",
              flush=True)
        last_scan = time.time()
        while duration is None or time.time() - started < duration:
            tick_start = time.time()
            self.tick()
            self.settle_resolved()
            if time.time() - last_scan > rescan_every:
                self.refresh_universe()
                if self.stream is not None:
                    self.stream.set_assets(self._tracked_tokens())
                last_scan = time.time()
            elapsed = time.time() - tick_start
            ws_state = ""
            if self.stream is not None:
                live = "live" if self.stream.connected else "reconnecting"
                ws_state = f" | ws {live} ({self._ws_books}ws/{self._rest_books}rest)"
            print(f"{time.strftime('%H:%M:%S')} tick {elapsed:.1f}s"
                  f" | cash ${self.broker.cash:.2f}"
                  f" | {len(self.broker.positions)} open{ws_state}", flush=True)
            time.sleep(max(0.0, interval - elapsed))
