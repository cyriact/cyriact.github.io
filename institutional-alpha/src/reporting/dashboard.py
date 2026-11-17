"""Interactive performance dashboard (single HTML file, Plotly from a pinned CDN build).

Reads outputs/tables/*.csv and writes outputs/reports/dashboard.html.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from src.common import ensure_dir, path

TAB = path("outputs") / "tables"
PRIMARY = ["A_block", "B_shareholding", "C_inst_earnings", "D_inst_block_earnings", "E_full"]


def _clean(o):
    if isinstance(o, float) and not np.isfinite(o):
        return None
    return o


def build() -> None:
    m = pd.read_csv(TAB / "monthly_returns_net.csv", index_col=0, parse_dates=True)
    perf = pd.read_csv(TAB / "strategy_performance.csv")
    dec = pd.read_csv(TAB / "decile_returns.csv")
    sub = pd.read_csv(TAB / "subperiod_performance.csv")
    series = [c for c in m.columns]
    data = {
        "dates": [d.strftime("%Y-%m-%d") for d in m.index],
        "series": {c: [_clean(float(x)) if pd.notna(x) else None for x in m[c].values] for c in series},
        "primary": PRIMARY + ["E_walk_forward"],
        "benchmarks": [c for c in ("Nifty 500", "Universe EW", "Nifty 50", "Nifty Midcap 150", "Nifty Smallcap 250") if c in m],
    }
    keep = perf[(perf.benchmark == "Nifty 500")]
    cols = ["strategy", "CAGR", "bench_CAGR", "excess_CAGR", "excess_CAGR_ci_lo", "excess_CAGR_ci_hi", "t_excess_nw",
            "ann_vol", "sharpe", "sortino", "max_dd", "calmar", "worst_month", "worst_12m", "hit_rate_vs_bench",
            "avg_turnover_m", "avg_cost_ann", "avg_holdings", "segment"]
    data["table"] = [{k: _clean(v) for k, v in r.items()} for r in keep[[c for c in cols if c in keep]].to_dict("records")]
    uew = perf[(perf.benchmark != "Nifty 500")][["strategy", "excess_CAGR", "t_excess_nw"]]
    data["vs_uew"] = {r.strategy: [_clean(r.excess_CAGR), _clean(r.t_excess_nw)] for r in uew.itertuples()}
    d = dec[dec.bucket != "Top-Bottom"].copy()
    d["bucket"] = d.bucket.astype(int)
    data["deciles"] = {}
    for (s, h), g in d.groupby(["score", "horizon"]):
        data["deciles"].setdefault(s.replace("score_", ""), {})[str(h)] = [_clean(float(x)) for x in g.sort_values("bucket")["mean"]]
    data["subperiods"] = [{k: _clean(v) for k, v in r.items()} for r in sub.to_dict("records")]
    html = TEMPLATE.replace("__DATA__", json.dumps(data, default=str))
    out = ensure_dir(path("outputs") / "reports") / "dashboard.html"
    out.write_text(html)


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Institutional Alpha Dashboard</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: one reading column of stacked chart panels, summary table first; NSE ledger feel (plex type, hairlines). */
:root {
  color-scheme: light;
  --bg: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10); --accent: #2a78d6;
  --good: #006300; --bad: #d03b3b;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --s5:#e87ba4; --s6:#008300; --s7:#6250d6; --s8:#e34948;
  --font: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  color-scheme: dark; --bg:#0d0d0d; --surface:#1a1a19; --ink:#f0efec; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border: rgba(255,255,255,0.10); --accent:#3987e5; --good:#0ca30c; --bad:#e66767;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#0ca30c; --s7:#9085e9; --s8:#e66767; } }
:root[data-theme="dark"] {
  color-scheme: dark; --bg:#0d0d0d; --surface:#1a1a19; --ink:#f0efec; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border: rgba(255,255,255,0.10); --accent:#3987e5; --good:#0ca30c; --bad:#e66767;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; --s6:#0ca30c; --s7:#9085e9; --s8:#e66767; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.5 var(--font);
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); padding-block: 24px 48px; }
main { max-width: 1120px; margin: 0 auto; display: grid; gap: 20px; }
header h1 { font-size: 1.6rem; font-weight: 600; margin: 0; text-wrap: balance; }
header p { color: var(--ink-2); margin: 6px 0 0; max-width: 72ch; }
.eyebrow { font: 500 0.72rem/1 var(--mono); letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
section.panel { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 16px; min-width: 0; }
section.panel h2 { font-size: 1rem; font-weight: 600; margin: 0 0 4px; }
section.panel .note { color: var(--ink-2); font-size: 0.85rem; margin: 0 0 10px; }
.chart { width: 100%; height: 380px; }
.row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; }
label { color: var(--ink-2); font-size: 0.85rem; }
select { font: inherit; color: var(--ink); background: var(--bg); border: 1px solid var(--axis); border-radius: 4px; padding: 4px 8px; }
select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.tablewrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; font-size: 0.82rem; }
th, td { text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--ink-2); font-weight: 500; position: sticky; top: 0; background: var(--surface); }
td.pos { color: var(--good); } td.neg { color: var(--bad); }
.foot { color: var(--muted); font-size: 0.78rem; }
</style>
</head>
<body>
<main>
<header>
  <div class="eyebrow">NSE · point-in-time backtest · monthly, net of costs</div>
  <h1>Institutional Alpha — strategy dashboard</h1>
  <p>Strategies A–E rank NSE stocks on institutional accumulation (quarterly shareholding XBRL, named &gt;1% holders, delivery-backed volume), earnings proxies and valuation. Benchmarks: approximate Nifty TRI (price index + NSE dividend yield) and an equal-weighted eligible universe.</p>
</header>

<section class="panel">
  <h2>Summary vs Nifty 500 TRI</h2>
  <p class="note">Full sample. Excess = strategy CAGR − benchmark CAGR; CI = stationary block bootstrap 95%; t = Newey–West on monthly excess returns. "vs UEW" = excess over the equal-weighted eligible universe.</p>
  <div class="row"><label for="tblFilter">Show</label>
    <select id="tblFilter"><option value="primary">Primary strategies</option><option value="segments">By market-cap segment</option><option value="variants">Variants &amp; robustness</option><option value="factors">Single signals &amp; weightings</option><option value="all">All</option></select></div>
  <div class="tablewrap"><table id="tbl"></table></div>
</section>

<section class="panel">
  <h2>Cumulative growth of ₹1</h2>
  <p class="note">Log scale. Click legend entries to hide or show series.</p>
  <div id="cum" class="chart"></div>
</section>

<section class="panel">
  <h2>Drawdowns</h2>
  <div id="dd" class="chart" style="height:300px"></div>
</section>

<section class="panel">
  <h2>Rolling 12-month return</h2>
  <div class="row"><label for="rollMode">Measure</label>
    <select id="rollMode"><option value="abs">Absolute</option><option value="xs">Excess vs Nifty 500</option></select></div>
  <div id="roll" class="chart" style="height:320px"></div>
</section>

<section class="panel">
  <h2>Calendar-year returns</h2>
  <div id="annual" class="chart" style="height:340px"></div>
</section>

<section class="panel">
  <h2>Return by score decile</h2>
  <p class="note">Mean forward excess return vs Nifty 500 of each score decile (10 = highest score), averaged over signal dates.</p>
  <div class="row"><label for="decSig">Signal</label><select id="decSig"></select>
    <label for="decH">Horizon</label><select id="decH"><option>1</option><option selected>3</option><option>6</option><option>12</option></select><span class="foot">months</span></div>
  <div id="dec" class="chart" style="height:300px"></div>
</section>

<section class="panel">
  <h2>Sub-periods</h2>
  <p class="note">Development 2018-07..2021-12, validation 2022-2023, out-of-sample 2024-01..2026-10.</p>
  <div class="tablewrap"><table id="subtbl"></table></div>
</section>
<p class="foot">Generated by run_pipeline.py. Data: NSE archives (bhavcopy, MTO delivery, corporate actions, index closes), NSE SHP XBRL filings, Yahoo annual fundamentals (supplementary, 2024+). Not investment advice.</p>
</main>
<script src="https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"></script>
<script>
const D = __DATA__;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const SER = () => [1,2,3,4,5,6,7,8].map(i => css('--s'+i));
const fmtP = (v, d=1) => v == null ? '–' : (v*100).toFixed(d) + '%';
const fmtN = (v, d=2) => v == null ? '–' : v.toFixed(d);
function layout(extra) {
  return Object.assign({
    paper_bgcolor: css('--surface'), plot_bgcolor: css('--surface'),
    font: {family: css('--font'), color: css('--ink-2'), size: 12},
    margin: {l: 56, r: 16, t: 8, b: 40}, hovermode: 'x unified',
    xaxis: {gridcolor: css('--grid'), linecolor: css('--axis'), zeroline: false},
    yaxis: {gridcolor: css('--grid'), linecolor: css('--axis'), zerolinecolor: css('--axis')},
    legend: {orientation: 'h', y: -0.15, font: {size: 11}},
  }, extra || {});
}
const CFG = {displaylogo: false, responsive: true, modeBarButtonsToRemove: ['lasso2d','select2d']};
function colorFor(name, i) {
  const s = SER();
  if (name === 'Nifty 500') return css('--ink');
  if (name === 'Universe EW') return css('--muted');
  return s[i % s.length];
}
function cumul(arr) { let v = 1; return arr.map(r => (r == null ? v : (v *= (1 + r)))); }
function ddser(arr) { let v = 1, pk = 1; return arr.map(r => { v *= 1 + (r || 0); pk = Math.max(pk, v); return v/pk - 1; }); }
function roll12(arr) { return arr.map((_, i) => { if (i < 11) return null; let p = 1; for (let k = i-11; k <= i; k++) { if (arr[k] == null) return null; p *= 1 + arr[k]; } return p - 1; }); }
const names = D.primary.filter(n => D.series[n]);
const bnames = ['Nifty 500', 'Universe EW'].filter(n => D.series[n]);
function lineTraces(fn, list) {
  return list.map((n, i) => ({x: D.dates, y: fn(D.series[n]), name: n, type: 'scatter', mode: 'lines',
    line: {width: (bnames.includes(n) ? 1.6 : 2), color: colorFor(n, i), dash: n === 'Nifty 500' ? 'dash' : (n === 'Universe EW' ? 'dot' : 'solid')},
    hovertemplate: '%{y:.3f}<extra>' + n + '</extra>'}));
}
function drawAll() {
  Plotly.react('cum', lineTraces(cumul, names.concat(bnames)), layout({yaxis: {type: 'log', gridcolor: css('--grid'), linecolor: css('--axis'), title: {text: 'growth of 1'}}}), CFG);
  Plotly.react('dd', lineTraces(ddser, ['D_inst_block_earnings', 'E_full', 'B_shareholding', 'Nifty 500'].filter(n => D.series[n])),
    layout({yaxis: {tickformat: '.0%', gridcolor: css('--grid'), linecolor: css('--axis')}}), CFG);
  drawRoll();
  drawAnnual();
  drawDec();
}
function drawRoll() {
  const mode = document.getElementById('rollMode').value;
  const nb = D.series['Nifty 500'];
  const list = ['B_shareholding', 'D_inst_block_earnings', 'E_full'].concat(mode === 'abs' ? ['Nifty 500'] : []).filter(n => D.series[n]);
  const tr = list.map((n, i) => {
    let y = roll12(D.series[n]);
    if (mode === 'xs') { const b = roll12(nb); y = y.map((v, k) => v == null || b[k] == null ? null : v - b[k]); }
    return {x: D.dates, y, name: n, type: 'scatter', mode: 'lines', line: {width: 2, color: colorFor(n, names.indexOf(n) >= 0 ? names.indexOf(n) : i), dash: n === 'Nifty 500' ? 'dash' : 'solid'},
      hovertemplate: '%{y:.1%}<extra>' + n + '</extra>'};
  });
  Plotly.react('roll', tr, layout({yaxis: {tickformat: '.0%', gridcolor: css('--grid'), linecolor: css('--axis'), zerolinecolor: css('--axis')}}), CFG);
}
function drawAnnual() {
  const years = [...new Set(D.dates.map(d => d.slice(0, 4)))];
  const list = names.concat(bnames);
  const tr = list.map((n, i) => {
    const y = years.map(yr => { let p = 1, c = 0; D.dates.forEach((d, k) => { if (d.startsWith(yr) && D.series[n][k] != null) { p *= 1 + D.series[n][k]; c++; } }); return c ? p - 1 : null; });
    return {x: years, y, name: n, type: 'bar', marker: {color: colorFor(n, i), line: {color: css('--surface'), width: 1}}, hovertemplate: '%{y:.1%}<extra>' + n + '</extra>'};
  });
  Plotly.react('annual', tr, layout({barmode: 'group', bargap: 0.25, hovermode: 'closest', yaxis: {tickformat: '.0%', gridcolor: css('--grid'), linecolor: css('--axis'), zerolinecolor: css('--axis')}}), CFG);
}
function drawDec() {
  const s = document.getElementById('decSig').value, h = document.getElementById('decH').value;
  const y = (D.deciles[s] || {})[h] || [];
  Plotly.react('dec', [{x: y.map((_, i) => i + 1), y, type: 'bar', name: s,
    marker: {color: y.map(v => v < 0 ? css('--s8') : css('--s1'))}, hovertemplate: 'decile %{x}: %{y:.2%}<extra></extra>'}],
    layout({hovermode: 'closest', showlegend: false, xaxis: {dtick: 1, title: {text: 'score decile'}, gridcolor: css('--grid'), linecolor: css('--axis')},
      yaxis: {tickformat: '.1%', gridcolor: css('--grid'), linecolor: css('--axis'), zerolinecolor: css('--axis')}}), CFG);
}
function rowClass(v) { return v == null ? '' : (v >= 0 ? 'pos' : 'neg'); }
function drawTable() {
  const f = document.getElementById('tblFilter').value;
  const isSeg = n => n.includes('|large') || n.includes('|mid') || n.includes('|small');
  const rows = D.table.filter(r => {
    const n = r.strategy;
    if (f === 'primary') return D.primary.includes(n) || n.startsWith('W_base');
    if (f === 'segments') return isSeg(n);
    if (f === 'variants') return n.includes('|') && !isSeg(n);
    if (f === 'factors') return n.startsWith('F_') || n.startsWith('W_') || n.includes('2024-07');
    return true;
  });
  const head = ['Strategy', 'CAGR', 'Bench', 'Excess', '95% CI', 't (NW)', 'vs UEW', 'Vol', 'Sharpe', 'Max DD', 'Worst 12m', 'Hit', 'Turnover/m', 'Cost/yr'];
  let h = '<thead><tr>' + head.map(x => '<th>' + x + '</th>').join('') + '</tr></thead><tbody>';
  rows.forEach(r => {
    const u = D.vs_uew[r.strategy] || [null, null];
    h += '<tr><td>' + r.strategy + '</td><td>' + fmtP(r.CAGR) + '</td><td>' + fmtP(r.bench_CAGR) + '</td><td class="' + rowClass(r.excess_CAGR) + '">' + fmtP(r.excess_CAGR) +
      '</td><td>' + (r.excess_CAGR_ci_lo == null ? '–' : fmtP(r.excess_CAGR_ci_lo) + ' … ' + fmtP(r.excess_CAGR_ci_hi)) + '</td><td>' + fmtN(r.t_excess_nw) +
      '</td><td class="' + rowClass(u[0]) + '">' + fmtP(u[0]) + '</td><td>' + fmtP(r.ann_vol) + '</td><td>' + fmtN(r.sharpe) + '</td><td>' + fmtP(r.max_dd) + '</td><td>' + fmtP(r.worst_12m) +
      '</td><td>' + fmtP(r.hit_rate_vs_bench, 0) + '</td><td>' + fmtP(r.avg_turnover_m, 0) + '</td><td>' + fmtP(r.avg_cost_ann) + '</td></tr>';
  });
  document.getElementById('tbl').innerHTML = h + '</tbody>';
}
function drawSub() {
  const head = ['Strategy', 'Period', 'Months', 'CAGR', 'Nifty 500', 'Excess', 't', 'UEW', 'Excess vs UEW', 'Sharpe', 'Max DD'];
  let h = '<thead><tr>' + head.map(x => '<th>' + x + '</th>').join('') + '</tr></thead><tbody>';
  D.subperiods.forEach(r => { h += '<tr><td>' + r.strategy + '</td><td>' + r.period + '</td><td>' + (r.months ?? '–') + '</td><td>' + fmtP(r.CAGR) + '</td><td>' + fmtP(r.N500_CAGR) +
    '</td><td class="' + rowClass(r.excess_vs_N500) + '">' + fmtP(r.excess_vs_N500) + '</td><td>' + fmtN(r.t_excess_vs_N500) + '</td><td>' + fmtP(r.UEW_CAGR) + '</td><td class="' + rowClass(r.excess_vs_UEW) + '">' + fmtP(r.excess_vs_UEW) +
    '</td><td>' + fmtN(r.sharpe) + '</td><td>' + fmtP(r.max_dd) + '</td></tr>'; });
  document.getElementById('subtbl').innerHTML = h + '</tbody>';
}
const decSel = document.getElementById('decSig');
Object.keys(D.deciles).forEach(k => { const o = document.createElement('option'); o.value = k; o.textContent = k; if (k === 'E_full') o.selected = true; decSel.appendChild(o); });
['decSig', 'decH'].forEach(id => document.getElementById(id).addEventListener('change', drawDec));
document.getElementById('rollMode').addEventListener('change', drawRoll);
document.getElementById('tblFilter').addEventListener('change', drawTable);
drawTable(); drawSub();
if (window.Plotly) { drawAll(); }
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => window.Plotly && drawAll());
new MutationObserver(() => window.Plotly && drawAll()).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
</script>
</body>
</html>
"""
