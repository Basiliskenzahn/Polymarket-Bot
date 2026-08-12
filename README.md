# pmquant — Prediction Market Paper-Trading System

A paper-trading framework for [Polymarket](https://polymarket.com) prediction
markets: real-time order-book collection, a market scanner, and a signal
engine with fee- and slippage-adjusted P&L simulation. Pure Python standard
library — no dependencies.

## Why prediction markets?

Binary prediction market contracts pay $1 if an event happens and $0
otherwise, so prices *are* probabilities. That makes them a clean sandbox for
the core ideas of quantitative trading: expected value, market microstructure,
order-book dynamics, and risk-managed position sizing.

## Architecture

```
Gamma API (metadata)      CLOB API (order books)
        │                          │
   scanner.py ──► engine.py ◄── api.py
                    │
     ┌──────────────┼───────────────┐
 signals.py    simulator.py     storage.py
 (imbalance,   (paper broker:   (SQLite:
  momentum,     walk-the-book    snapshots,
  dutch book)   fills, fees)     trades, P&L)
```

- **Scanner** — pulls active binary markets from the Gamma API, filters by
  liquidity/volume, and skips near-resolved markets (price pinned at 0/1).
- **Collector** — maintains live order books over Polymarket's CLOB websocket
  (a hand-rolled RFC 6455 client: handshake, frame masking, ping/pong,
  reconnect with backoff — no dependencies), applying `book` snapshots and
  `price_change` deltas; falls back to REST polling per market whenever the
  socket drops or a book goes stale. Each tick stores best bid/ask, depth,
  imbalance, and top-5 levels in SQLite.
- **Signal engine** —
  - *dutch book*: YES + NO asks summing below $1 → risk-free arbitrage pair;
  - *order-book imbalance*: depth-weighted buy/sell pressure near the touch;
  - *momentum*: mid-price drift as a confirmation filter.
- **Paper broker** — market orders are filled by walking the *real* order
  book level by level, so simulated fills pay real slippage. Fees follow
  Polymarket's schedule `fee = rate · min(p, 1−p) · shares` (base rate
  configurable; currently 0 on most markets). No shorting exists on
  prediction markets, so "sell YES" is expressed as "buy NO".
- **Settlement** — positions in resolved markets are settled at the $0/$1
  outcome, closing the P&L loop.

## Usage

```bash
python3 -m pmquant.cli scan                 # show the current tradeable universe
python3 -m pmquant.cli run --top 15         # collect + paper trade until Ctrl-C
python3 -m pmquant.cli run --minutes 60     # timed session (--no-ws for REST only)
python3 -m pmquant.cli report               # markets, snapshots, trades, P&L
python3 -m pmquant.cli analyze              # does imbalance predict 1–5 min moves?
```

`analyze` evaluates the imbalance signal on the collected snapshot history:
Pearson correlation (with t-statistic), directional hit rate, and mean
forward mid-price move in the bottom vs top imbalance tercile, at 60s and
300s horizons. Collection gaps (laptop asleep) are excluded from the sample.

State (order-book history, trades, account equity) persists in
`data/pmquant.db`, so the broker resumes cash and open positions across
sessions.

## Honest limitations

- Paper fills assume our order doesn't move the market (fine at small size).
- Books update in real time over the websocket, but signals are sampled and
  traded on a ~30s tick, so sub-tick alpha is invisible by design.
- The included signals are transparent baselines, not production alpha.
