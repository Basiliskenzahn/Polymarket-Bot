"""Command-line interface.

    python -m pmquant.cli scan            # show current tradeable universe
    python -m pmquant.cli run             # collect + paper trade (Ctrl-C to stop)
    python -m pmquant.cli report          # P&L and activity summary
"""
from __future__ import annotations

import argparse
import os

from .analysis import evaluate
from .api import GammaClient
from .engine import Config, Engine
from .scanner import scan
from .simulator import PaperBroker
from .storage import Store

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data")
DB_PATH = os.path.join(_DATA_DIR, "pmquant.db")
TICKS_PATH = os.path.join(_DATA_DIR, "ticks.db")


def cmd_scan(args: argparse.Namespace) -> None:
    markets = scan(GammaClient(), top=args.top)
    print(f"{'24h volume':>12}  {'liquidity':>12}  {'price':>6}  question")
    for m in markets:
        px = m.outcome_prices[0] if m.outcome_prices else float("nan")
        print(f"{m.volume24h:>12,.0f}  {m.liquidity:>12,.0f}  {px:>6.3f}  "
              f"{m.question[:70]}")


def cmd_run(args: argparse.Namespace) -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    store = Store(DB_PATH)
    engine = Engine(store, PaperBroker(cash=args.cash), Config(top_markets=args.top),
                    use_ws=not args.no_ws,
                    ticks_path=None if args.no_ws else TICKS_PATH)
    duration = args.minutes * 60 if args.minutes else None
    try:
        engine.run(interval=args.interval, duration=duration)
    except KeyboardInterrupt:
        print("\nstopped.")


def cmd_analyze(args: argparse.Namespace) -> None:
    store = Store(DB_PATH)
    evaluate(store.conn, [float(h) for h in args.horizons.split(",")])


def cmd_ticks(args: argparse.Namespace) -> None:
    import sqlite3
    if not os.path.exists(TICKS_PATH):
        print("no tick data yet — start a run first")
        return
    conn = sqlite3.connect(TICKS_PATH)
    total, t0, t1, assets = conn.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts), COUNT(DISTINCT asset) FROM ticks"
    ).fetchone()
    if not total:
        print("tick file exists but is empty")
        return
    hours = (t1 - t0) / 3600
    size_mb = sum(os.path.getsize(TICKS_PATH + ext)
                  for ext in ("", "-wal") if os.path.exists(TICKS_PATH + ext)) / 1e6
    print("== tick capture ==")
    print(f"events:   {total:,} over {hours:.1f}h "
          f"({total / max(1e-9, t1 - t0):.1f}/s avg) across {assets} tokens")
    print(f"on disk:  {size_mb:.1f} MB "
          f"(~{size_mb / max(1e-3, hours) * 24:.0f} MB/day at this rate)")
    for kind, n in conn.execute(
            "SELECT kind, COUNT(*) FROM ticks GROUP BY kind ORDER BY 2 DESC"):
        print(f"  {kind:<10} {n:>10,}")


def cmd_flow(args: argparse.Namespace) -> None:
    from .tickstudy import run_study
    if not os.path.exists(TICKS_PATH):
        print("no tick data yet — start a run first")
        return
    run_study(TICKS_PATH, DB_PATH)


def cmd_maker(args: argparse.Namespace) -> None:
    from .makersim import run_maker
    if not os.path.exists(TICKS_PATH):
        print("no tick data yet — start a run first")
        return
    run_maker(TICKS_PATH, DB_PATH)


def cmd_chart(args: argparse.Namespace) -> None:
    from .charts import render_standalone
    out = os.path.join(_DATA_DIR, "report.html")
    with open(out, "w") as fh:
        fh.write(render_standalone(DB_PATH, TICKS_PATH))
    print(f"dashboard written to {out}")


def cmd_report(args: argparse.Namespace) -> None:
    store = Store(DB_PATH)
    c = store.conn
    n_markets = c.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    n_snaps, t0, t1 = c.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM snapshots").fetchone()
    n_trades, volume, fees = c.execute(
        "SELECT COUNT(*), COALESCE(SUM(shares*avg_price),0), COALESCE(SUM(fee),0)"
        " FROM trades WHERE side != 'settle'").fetchone()
    first = c.execute("SELECT equity FROM account ORDER BY ts LIMIT 1").fetchone()
    last = c.execute("SELECT * FROM account ORDER BY ts DESC LIMIT 1").fetchone()
    hours = (t1 - t0) / 3600 if n_snaps else 0

    print("== pmquant paper-trading report ==")
    print(f"markets tracked:     {n_markets}")
    print(f"book snapshots:      {n_snaps:,} over {hours:.1f}h")
    print(f"simulated trades:    {n_trades}  (${volume:,.2f} notional, "
          f"${fees:.2f} fees)")
    if first and last:
        pnl = last["equity"] - first["equity"]
        print(f"equity:              ${last['equity']:.2f}  "
              f"(cash ${last['cash']:.2f} + positions ${last['position_value']:.2f})")
        print(f"P&L since start:     ${pnl:+.2f}")
    for row in c.execute(
            "SELECT signal, COUNT(*) n, SUM(shares*avg_price) vol FROM trades"
            " WHERE side != 'settle' GROUP BY signal"):
        print(f"  {row['signal']:<12} {row['n']:>4} trades  ${row['vol']:,.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="pmquant")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="show current tradeable universe")
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("run", help="collect order books and paper trade")
    p.add_argument("--top", type=int, default=15, help="markets to track")
    p.add_argument("--cash", type=float, default=1000.0, help="starting paper USDC")
    p.add_argument("--interval", type=float, default=30.0, help="seconds per tick")
    p.add_argument("--minutes", type=float, default=None,
                   help="stop after N minutes (default: run until Ctrl-C)")
    p.add_argument("--no-ws", action="store_true",
                   help="disable the websocket feed, poll REST only")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("analyze",
                       help="evaluate imbalance signal vs forward mid moves")
    p.add_argument("--horizons", default="60,300",
                   help="comma-separated horizons in seconds (default 60,300)")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("report", help="P&L and activity summary")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("ticks", help="tick capture statistics")
    p.set_defaults(func=cmd_ticks)

    p = sub.add_parser("chart", help="render the HTML dashboard to data/report.html")
    p.set_defaults(func=cmd_chart)

    p = sub.add_parser("flow", help="trade-flow predictiveness study on tick data")
    p.set_defaults(func=cmd_flow)

    p = sub.add_parser("maker", help="passive market-making backtest on tick data")
    p.set_defaults(func=cmd_maker)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
