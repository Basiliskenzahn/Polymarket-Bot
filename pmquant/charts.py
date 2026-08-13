"""Dashboard renderer: a self-contained HTML report from the SQLite data.

No external assets — CSS, SVG charts, and the tooltip script are all inline,
so the file works offline and can be regenerated at any time with
`python3 -m pmquant.cli chart`.
"""
from __future__ import annotations

import html
import json
import math
import os
import sqlite3
import time
from typing import Callable, List, Optional, Sequence, Tuple

from .analysis import _forward_pairs, _pearson

# -- css / js (theme tokens per the reference dataviz palette) -----------------

_CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e;
  --muted: #898781; --grid: #e1e0d9; --baseline: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --s1: #2a78d6; --s2: #eb6834; --good: #006300; --bad: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --baseline: #383835;
    --border: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --good: #0ca30c; --bad: #d03b3b;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
  --muted: #898781; --grid: #2c2c2a; --baseline: #383835;
  --border: rgba(255,255,255,0.10);
  --s1: #3987e5; --s2: #d95926; --good: #0ca30c; --bad: #d03b3b;
}
body {
  background: var(--page); color: var(--ink); margin: 0;
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 28px 20px 48px; }
header h1 { font-size: 20px; font-weight: 650; margin: 0; }
header p { color: var(--ink-2); margin: 4px 0 0; }
section { margin-top: 32px; }
h2 { font-size: 15px; font-weight: 650; margin: 0 0 2px; }
.sub { color: var(--muted); font-size: 12.5px; margin: 0 0 12px; }
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px;
}
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
         gap: 10px; margin-top: 20px; }
.tile { background: var(--surface); border: 1px solid var(--border);
        border-radius: 8px; padding: 12px 14px; }
.tile .lbl { color: var(--muted); font-size: 12px; }
.tile .val { font-size: 22px; font-weight: 600; margin-top: 2px; }
.tile .d { font-size: 12px; margin-top: 2px; color: var(--ink-2); }
.tile .d.up { color: var(--good); } .tile .d.down { color: var(--bad); }
.legend { display: flex; gap: 16px; font-size: 12.5px; color: var(--ink-2);
          margin-bottom: 8px; }
.legend .k { display: inline-block; width: 14px; height: 3px; border-radius: 2px;
             vertical-align: middle; margin-right: 6px; }
.k1 { background: var(--s1); } .k2 { background: var(--s2); }
svg { display: block; width: 100%; height: auto; }
svg text { font: 11px system-ui, -apple-system, "Segoe UI", sans-serif;
           fill: var(--muted); font-variant-numeric: tabular-nums; }
svg text.pt { font-size: 12px; fill: var(--ink); font-weight: 600; }
.grid-ln { stroke: var(--grid); stroke-width: 1; }
.base-ln { stroke: var(--baseline); stroke-width: 1; }
.ln1 { stroke: var(--s1); } .ln2 { stroke: var(--s2); }
.ln { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.dot1 { fill: var(--s1); } .dot2 { fill: var(--s2); }
.ring { stroke: var(--surface); stroke-width: 2; }
.sc { fill: var(--s1); fill-opacity: 0.75; stroke: var(--surface); stroke-width: 1.5; }
.xhair { stroke: var(--baseline); stroke-width: 1; display: none; }
.focus { display: none; }
.panels { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
          gap: 10px; }
.panel { background: var(--surface); border: 1px solid var(--border);
         border-radius: 8px; padding: 10px 12px 6px; }
.panel .q { font-size: 12px; color: var(--ink-2); margin-bottom: 4px;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tbl-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th { text-align: left; color: var(--muted); font-weight: 500; font-size: 12px;
     padding: 6px 10px; border-bottom: 1px solid var(--grid); }
td { padding: 6px 10px; border-bottom: 1px solid var(--grid);
     font-variant-numeric: tabular-nums; white-space: nowrap; }
td.q { max-width: 320px; overflow: hidden; text-overflow: ellipsis; }
#tip { position: fixed; display: none; background: var(--surface); color: var(--ink);
       border: 1px solid var(--border); border-radius: 6px; padding: 7px 10px;
       font: 12px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
       pointer-events: none; box-shadow: 0 2px 10px rgba(0,0,0,0.12); z-index: 10;
       font-variant-numeric: tabular-nums; }
#tip .t { color: var(--muted); }
#tip .k { display: inline-block; width: 8px; height: 8px; border-radius: 2px;
          vertical-align: baseline; margin-right: 5px; }
footer { margin-top: 36px; color: var(--muted); font-size: 12px; }
code { background: var(--surface); border: 1px solid var(--border);
       border-radius: 4px; padding: 1px 5px; font-size: 12px; }
"""

_JS = """
(function () {
  var tip = document.getElementById('tip');
  document.querySelectorAll('svg[data-pts]').forEach(function (svg) {
    var pts = JSON.parse(svg.getAttribute('data-pts'));
    var mode = svg.getAttribute('data-mode') || 'x';
    var vw = svg.viewBox.baseVal.width;
    var xhair = svg.querySelector('.xhair');
    var focus = svg.querySelectorAll('.focus');
    function hide() {
      tip.style.display = 'none';
      if (xhair) xhair.style.display = 'none';
      focus.forEach(function (c) { c.style.display = 'none'; });
    }
    svg.addEventListener('mouseleave', hide);
    svg.addEventListener('mousemove', function (e) {
      var r = svg.getBoundingClientRect();
      var mx = (e.clientX - r.left) / r.width * vw;
      var my = (e.clientY - r.top) / r.height * svg.viewBox.baseVal.height;
      var best = null, bd = Infinity;
      pts.forEach(function (p) {
        var d = mode === 'x' ? Math.abs(p.x - mx)
                             : Math.hypot(p.x - mx, p.y - my);
        if (d < bd) { bd = d; best = p; }
      });
      if (!best || (mode === 'xy' && bd > 30)) { hide(); return; }
      if (xhair) {
        xhair.setAttribute('x1', best.x); xhair.setAttribute('x2', best.x);
        xhair.style.display = '';
      }
      focus.forEach(function (c, i) {
        var y = best.ys ? best.ys[i] : best.y;
        if (y == null) { c.style.display = 'none'; return; }
        c.setAttribute('cx', best.x); c.setAttribute('cy', y);
        c.style.display = '';
      });
      tip.innerHTML = best.h;
      tip.style.display = 'block';
      var x = e.clientX + 14;
      if (x + tip.offsetWidth > window.innerWidth - 8)
        x = e.clientX - tip.offsetWidth - 14;
      tip.style.left = x + 'px';
      tip.style.top = (e.clientY + 14) + 'px';
    });
  });
})();
"""

# -- small numeric helpers ------------------------------------------------------


def _fmt_money(v: float) -> str:
    return f"${v:,.2f}"


def _fmt_px(v: float) -> str:
    return f"{v:.3f}"


def _compact(n: float) -> str:
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= cut:
            return f"{n / cut:.1f}{suffix}"
    return f"{n:,.0f}"


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _ticks(lo: float, hi: float, n: int = 4) -> List[float]:
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    first = math.ceil(lo / step) * step
    out = []
    v = first
    while v <= hi + 1e-9:
        out.append(round(v, 10))
        v += step
    return out or [lo]


def _time_ticks(t0: float, t1: float) -> List[float]:
    for step in (300, 600, 900, 1800, 3600, 7200, 14400, 43200):
        if (t1 - t0) / step <= 6:
            break
    first = math.ceil(t0 / step) * step
    return [t for t in range(int(first), int(t1) + 1, step)]


def _segments(times: Sequence[float], gap: float) -> List[Tuple[int, int]]:
    """Index ranges of contiguous runs, split where sampling gapped (sim off)."""
    segs, start = [], 0
    for i in range(1, len(times)):
        if times[i] - times[i - 1] > gap:
            segs.append((start, i))
            start = i
    segs.append((start, len(times)))
    return segs


def _attr(payload) -> str:
    return html.escape(json.dumps(payload, separators=(",", ":")), quote=True)


def _stride(rows: list, target: int) -> list:
    """Evenly downsample to ~target items, always keeping the last one."""
    if len(rows) <= target:
        return rows
    step = len(rows) / target
    out = [rows[int(i * step)] for i in range(target)]
    if out[-1] is not rows[-1]:
        out.append(rows[-1])
    return out


# -- chart builders --------------------------------------------------------------

PAD_L, PAD_R, PAD_T, PAD_B = 56, 14, 12, 24


def _line_chart(times: List[float], series: List[Tuple[str, str, List[Optional[float]]]],
                width: int, height: int,
                y_fmt: Callable[[float], str] = _fmt_money,
                axis_fmt: Optional[Callable[[float], str]] = None,
                show_axes: bool = True) -> str:
    """Aligned multi-series line chart. series = [(name, class_suffix, values)]."""
    vals = [v for _, _, vs in series for v in vs if v is not None]
    if len(times) < 2 or not vals:
        return "<p class='sub'>not enough data yet</p>"
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or abs(hi) * 0.01 or 0.01
    lo, hi = lo - pad, hi + pad
    t0, t1 = times[0], times[-1]
    px = lambda t: PAD_L + (t - t0) / (t1 - t0) * (width - PAD_L - PAD_R)
    py = lambda v: PAD_T + (hi - v) / (hi - lo) * (height - PAD_T - PAD_B)
    med = sorted(times[i] - times[i - 1] for i in range(1, len(times)))[len(times) // 2]
    gap = max(120.0, med * 5)

    parts = [f'<svg viewBox="0 0 {width} {height}" data-mode="x" data-pts="%PTS%">']
    axis_fmt = axis_fmt or y_fmt
    if show_axes:
        for tv in _ticks(lo, hi):
            y = py(tv)
            parts.append(f'<line class="grid-ln" x1="{PAD_L}" y1="{y:.1f}"'
                         f' x2="{width - PAD_R}" y2="{y:.1f}"/>')
            parts.append(f'<text x="{PAD_L - 8}" y="{y + 3.5:.1f}"'
                         f' text-anchor="end">{axis_fmt(tv)}</text>')
        for tt in _time_ticks(t0, t1):
            parts.append(f'<text x="{px(tt):.1f}" y="{height - 6}"'
                         f' text-anchor="middle">{_hhmm(tt)}</text>')
    parts.append(f'<line class="base-ln" x1="{PAD_L}" y1="{height - PAD_B}"'
                 f' x2="{width - PAD_R}" y2="{height - PAD_B}"/>')
    parts.append(f'<line class="xhair" y1="{PAD_T}" y2="{height - PAD_B}" x1="0" x2="0"/>')

    for name, cls, values in series:
        for a, b in _segments(times, gap):
            pts = [(px(times[i]), py(values[i]))
                   for i in range(a, b) if values[i] is not None]
            if len(pts) < 2:
                continue
            d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            parts.append(f'<path class="ln ln{cls}" d="{d}"/>')
        last = next((i for i in range(len(values) - 1, -1, -1)
                     if values[i] is not None), None)
        if last is not None:
            parts.append(f'<circle class="dot{cls} ring" r="4"'
                         f' cx="{px(times[last]):.1f}" cy="{py(values[last]):.1f}"/>')
    for _, cls, _v in series:
        parts.append(f'<circle class="focus dot{cls} ring" r="4" cx="0" cy="0"/>')

    hover = []
    for i, t in enumerate(times):
        rows = "".join(
            f'<div><span class="k" style="background:var(--s{cls})"></span>'
            f'{html.escape(name)} <b>{y_fmt(values[i])}</b></div>'
            for name, cls, values in series if values[i] is not None)
        label = time.strftime("%H:%M:%S", time.localtime(t))
        hover.append({
            "x": round(px(t), 1),
            "ys": [round(py(vs[i]), 1) if vs[i] is not None else None
                   for _, _, vs in series],
            "h": f'<div class="t">{label}</div>{rows}',
        })
    parts.append("</svg>")
    return "".join(parts).replace("%PTS%", _attr(hover))


def _scatter(pairs: List[Tuple[float, float]], width: int = 520,
             height: int = 300) -> str:
    if len(pairs) < 10:
        return "<p class='sub'>not enough matched pairs yet</p>"
    ys = sorted(abs(y) for _, y in pairs)
    y_cap = max(ys[int(len(ys) * 0.99) - 1], 0.001)  # clip the top 1% of |Δmid|
    shown = _stride([(x, y) for x, y in pairs if abs(y) <= y_cap], 800)
    px = lambda x: PAD_L + (x + 1) / 2 * (width - PAD_L - PAD_R)
    py = lambda y: PAD_T + (y_cap - y) / (2 * y_cap) * (height - PAD_T - PAD_B)
    parts = [f'<svg viewBox="0 0 {width} {height}" data-mode="xy" data-pts="%PTS%">']
    for tv in _ticks(-y_cap, y_cap):
        parts.append(f'<line class="grid-ln" x1="{PAD_L}" y1="{py(tv):.1f}"'
                     f' x2="{width - PAD_R}" y2="{py(tv):.1f}"/>')
        parts.append(f'<text x="{PAD_L - 8}" y="{py(tv) + 3.5:.1f}"'
                     f' text-anchor="end">{tv:+.3f}</text>')
    for xv in (-1, -0.5, 0, 0.5, 1):
        parts.append(f'<text x="{px(xv):.1f}" y="{height - 6}"'
                     f' text-anchor="middle">{xv:+g}</text>')
    parts.append(f'<line class="base-ln" x1="{px(0):.1f}" y1="{PAD_T}"'
                 f' x2="{px(0):.1f}" y2="{height - PAD_B}"/>')
    parts.append(f'<line class="base-ln" x1="{PAD_L}" y1="{py(0):.1f}"'
                 f' x2="{width - PAD_R}" y2="{py(0):.1f}"/>')
    hover = []
    for x, y in shown:
        parts.append(f'<circle class="sc" r="3.5" cx="{px(x):.1f}" cy="{py(y):.1f}"/>')
        hover.append({"x": round(px(x), 1), "y": round(py(y), 1),
                      "h": f'imbalance <b>{x:+.2f}</b><br>Δmid 60s <b>{y:+.4f}</b>'})
    parts.append(f'<circle class="focus dot1 ring" r="5" cx="0" cy="0"/>')
    parts.append("</svg>")
    return "".join(parts).replace("%PTS%", _attr(hover))


# -- page assembly ----------------------------------------------------------------


def _tile(label: str, value: str, detail: str = "", cls: str = "") -> str:
    d = f'<div class="d {cls}">{detail}</div>' if detail else ""
    return (f'<div class="tile"><div class="lbl">{html.escape(label)}</div>'
            f'<div class="val">{value}</div>{d}</div>')


def render_body(db_path: str, ticks_path: Optional[str] = None) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    now = time.strftime("%Y-%m-%d %H:%M", time.localtime())

    account = _stride(conn.execute(
        "SELECT ts, cash, position_value, equity FROM account ORDER BY ts"
    ).fetchall(), 500)
    n_markets = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    n_snaps = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    n_trades, fees = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(fee),0) FROM trades"
        " WHERE side != 'settle'").fetchone()
    n_open = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]

    # stat tiles
    tiles = []
    if account:
        eq0, eq1 = account[0]["equity"], account[-1]["equity"]
        pnl = eq1 - eq0
        tiles.append(_tile("Equity", _fmt_money(eq1),
                           f"{'▲' if pnl >= 0 else '▼'} {_fmt_money(abs(pnl))}"
                           f" vs session start",
                           "up" if pnl >= 0 else "down"))
        tiles.append(_tile("Cash / positions",
                           _fmt_money(account[-1]["cash"]),
                           f"+ {_fmt_money(account[-1]['position_value'])} held"))
    tiles.append(_tile("Markets tracked", str(n_markets),
                       f"{n_open} open positions"))
    tiles.append(_tile("Book snapshots", _compact(n_snaps)))
    tiles.append(_tile("Paper trades", str(n_trades),
                       f"{_fmt_money(fees)} fees"))
    if ticks_path and os.path.exists(ticks_path):
        tconn = sqlite3.connect(ticks_path)
        nt, tt0, tt1 = tconn.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM ticks").fetchone()
        if nt:
            tiles.append(_tile("Tick events", _compact(nt),
                               f"{nt / max(1e-9, tt1 - tt0):.1f}/s avg"))
        tconn.close()

    # equity chart
    eq_chart = _line_chart(
        [r["ts"] for r in account],
        [("Equity", "1", [r["equity"] for r in account]),
         ("Cash", "2", [r["cash"] for r in account])],
        width=1040, height=250, axis_fmt=lambda v: f"${v:,.0f}")

    # per-market mid panels (top 6 by snapshot count)
    panels = []
    top = conn.execute(
        "SELECT m.id, m.question, m.token_yes, COUNT(*) n FROM snapshots s"
        " JOIN markets m ON s.token_id = m.token_yes"
        " GROUP BY m.id ORDER BY n DESC LIMIT 6").fetchall()
    for row in top:
        snaps = _stride(conn.execute(
            "SELECT ts, mid FROM snapshots WHERE token_id = ? AND mid IS NOT NULL"
            " ORDER BY ts", (row["token_yes"],)).fetchall(), 200)
        chart = _line_chart([s["ts"] for s in snaps],
                            [("YES mid", "1", [s["mid"] for s in snaps])],
                            width=340, height=110, y_fmt=_fmt_px)
        panels.append(f'<div class="panel"><div class="q"'
                      f' title="{html.escape(row["question"])}">'
                      f'{html.escape(row["question"])}</div>{chart}</div>')

    # imbalance vs forward move
    pairs: List[Tuple[float, float]] = []
    for (token,) in conn.execute("SELECT token_yes FROM markets"):
        rows = conn.execute(
            "SELECT ts, mid, imbalance FROM snapshots WHERE token_id = ?"
            " ORDER BY ts", (token,)).fetchall()
        pairs += _forward_pairs([(r[0], r[1], r[2]) for r in rows], 60)
    if len(pairs) >= 30:
        r = _pearson([p[0] for p in pairs], [p[1] for p in pairs]) or 0.0
        t_stat = r * math.sqrt((len(pairs) - 2) / max(1e-12, 1 - r * r))
        verdict = ("statistically significant" if abs(t_stat) > 2
                   else "not statistically significant yet")
        sig_line = (f"n = {len(pairs):,} pairs · r = {r:+.3f} · t = {t_stat:+.2f}"
                    f" — {verdict}")
    else:
        sig_line = f"n = {len(pairs)} pairs — keep collecting"
    scatter = _scatter(pairs)

    # recent trades table
    trade_rows = conn.execute(
        "SELECT t.ts, t.side, t.signal, t.shares, t.avg_price, t.slippage,"
        " m.question FROM trades t LEFT JOIN markets m ON t.market_id = m.id"
        " WHERE t.side != 'settle' ORDER BY t.ts DESC LIMIT 12").fetchall()
    trades_html = "".join(
        f"<tr><td>{time.strftime('%H:%M:%S', time.localtime(r['ts']))}</td>"
        f"<td class='q' title=\"{html.escape(r['question'] or '')}\">"
        f"{html.escape((r['question'] or '?'))}</td>"
        f"<td>{r['side'].upper()}</td><td>{r['signal']}</td>"
        f"<td>{r['shares']:.1f}</td><td>{r['avg_price']:.3f}</td>"
        f"<td>{r['slippage']:+.4f}</td></tr>"
        for r in trade_rows)
    conn.close()

    return f"""
<div class="wrap">
  <header>
    <h1>pmquant — paper trading dashboard</h1>
    <p>Polymarket prediction markets · data through {now} · regenerate with
       <code>python3 -m pmquant.cli chart</code></p>
  </header>

  <div class="tiles">{''.join(tiles)}</div>

  <section>
    <h2>Equity &amp; cash</h2>
    <p class="sub">Paper account over time; the gap between the lines is
       capital deployed in positions. Line breaks are collection gaps.</p>
    <div class="card">
      <div class="legend"><span><span class="k k1"></span>Equity</span>
        <span><span class="k k2"></span>Cash</span></div>
      {eq_chart}
    </div>
  </section>

  <section>
    <h2>Tracked markets — YES mid-price</h2>
    <p class="sub">Six most-observed markets. Prices are probabilities (0–1).</p>
    <div class="panels">{''.join(panels)}</div>
  </section>

  <section>
    <h2>Does order-book imbalance predict the next move?</h2>
    <p class="sub">Each dot: imbalance now (x) vs mid-price change 60&thinsp;s
       later (y), pooled across markets. {sig_line}</p>
    <div class="card">{scatter}</div>
  </section>

  <section>
    <h2>Recent trades</h2>
    <p class="sub">Last {len(trade_rows)} simulated fills (walked against the
       live book; slippage is signed vs pre-trade mid).</p>
    <div class="card tbl-wrap"><table>
      <thead><tr><th>Time</th><th>Market</th><th>Side</th><th>Signal</th>
      <th>Shares</th><th>Avg price</th><th>Slippage</th></tr></thead>
      <tbody>{trades_html}</tbody>
    </table></div>
  </section>

  <footer>pmquant · paper trading only — no real orders are placed.</footer>
</div>
<div id="tip"></div>
"""


def render_standalone(db_path: str, ticks_path: Optional[str] = None) -> str:
    body = render_body(db_path, ticks_path)
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>pmquant dashboard</title><style>{_CSS}</style></head>"
            f"<body>{body}<script>{_JS}</script></body></html>")
