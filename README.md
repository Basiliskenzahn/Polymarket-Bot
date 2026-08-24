# pmquant: Market Microstructure Research & Paper-Trading System

A paper-trading and microstructure research framework: real-time order-book
collection, a market scanner, a signal engine with fee- and slippage-adjusted
P&L simulation, tick-level capture, and replay-based studies (trade-flow
predictiveness, passive market-making backtests). Pure Python standard
library — no dependencies, including a hand-rolled RFC 6455 websocket client.

Two venues share one pipeline:

- **[Polymarket](https://polymarket.com)** prediction markets (original
  target; binary contracts whose prices are probabilities). Swiss ISPs
  DNS-block the domain under the Money Gaming Act, so collection from a
  Swiss connection is unreliable — the dataset collected before the block
  remains fully usable.
- **Binance** spot crypto (`--venue crypto`): top-5 books at 1s + aggregated
  trade prints for the highest-volume USDT pairs — ~100× Polymarket's trade
  print rate, so studies reach statistical significance in hours, not weeks.

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
- **Tick capture** — every websocket event is also persisted to
  `data/ticks.db` for research. Polymarket emits full book snapshots, so the
  store delta-encodes the stream itself: one flat row per changed price level
  (diffed against the previous snapshot), trade prints with aggressor side,
  and a zlib-compressed full-book checkpoint per token every 10 minutes to
  bound replay length. Token ids (~77-digit strings) are interned to integer
  keys. Net cost: ~70 bytes/event — a few hundred MB/day even during in-play
  sports bursts. The book at any time t = latest checkpoint ≤ t + replayed
  changes, enabling order-flow research at full tick resolution.
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
python3 -m pmquant.cli ticks                # tick capture stats (rate, size, kinds)
python3 -m pmquant.cli flow                 # replay ticks: does trade flow predict
                                            # 1-60s mid moves? (momentum/reversal)
python3 -m pmquant.cli maker                # passive market-making backtest
                                            # (naive / fade / chase variants)
python3 -m pmquant.cli crypto --top 5       # collect Binance books + trades

# every analysis command accepts --venue crypto to run on the Binance data:
python3 -m pmquant.cli --venue crypto flow
python3 -m pmquant.cli --venue crypto maker
```

`analyze` evaluates the imbalance signal on the collected snapshot history:
Pearson correlation (with t-statistic), directional hit rate, and mean
forward mid-price move in the bottom vs top imbalance tercile, at 60s and
300s horizons. Collection gaps (laptop asleep) are excluded from the sample.

State (order-book history, trades, account equity) persists in
`data/pmquant.db`, so the broker resumes cash and open positions across
sessions.

## Findings so far (updated 24 Aug 2026)

Based on 12 days of Binance ticks (7.1M trade prints, 9 pairs) and 8 days of
Polymarket ticks (32k prints, 171 tokens).

- **Taker strategies can't clear costs.** The order-book-imbalance baseline
  loses at exactly the rate spread/slippage predicts (paper P&L −$637 over
  753 fills); episodes of positive P&L decomposed into resolution luck on a
  handful of binary markets, not edge.
- **Trade flow predicts short-horizon moves, and the effect is momentum
  that decays — not momentum-then-reversal.** On Binance, 10s taker flow
  correlates with the next-second return at r = +0.15 (65% directional hit
  rate), fading monotonically to r = +0.06 at 60s, with the same shape on
  every pair. Part of the 1s number is the print's own mechanical price
  impact (books are 1s snapshots). On Polymarket the same measurement is
  weakly positive at every horizon (r ≈ +0.03–0.04, hit 54–57%).
- **A one-day finding did not replicate.** The first Polymarket sample
  (one day, sports-heavy) showed a significant *reversal* at 15–60s
  (t ≈ −3 to −5). With 8× the data it is gone. Kept here deliberately: it's
  what small-sample "significance" looks like from the inside.
- **The signal is fee-gated.** Strong flow predicts a few bps of move;
  retail taker fees are ~10 bps. It's a market-maker's quote-skew input, not
  a taker strategy — the fee structure decides who can harvest it.
- **Passive quoting loses to adverse selection (negative result).** The
  strict-through-fill maker backtest on Binance earns +$1.6k of spread at
  fill and gives back −$22.7k of post-fill drift over 1.4M fills. Fade and
  chase variants are indistinguishable — there is no reversal to fade. The
  sim is also structurally unfair to the maker: "through" fills against a
  1s-stale snapshot are adverse by construction; a credible version needs
  the `depth@100ms` diff stream.

Caveats: t-stats in `tickstudy.py` assume independent samples and are
inflated by overlapping windows — read r and hit rate, not t. Numbers are
re-verified against fresh data before being quoted anywhere.

## Honest limitations

- Paper fills assume our order doesn't move the market (fine at small size).
- Books update in real time over the websocket, but signals are sampled and
  traded on a ~30s tick, so sub-tick alpha is invisible by design.
- The included signals are transparent baselines, not production alpha.
