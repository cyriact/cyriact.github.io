"""Event-study reconstructions for SAILIFE (2025), ETERNAL/ZOMATO (Jul-2024) and MEESHO (Nov-2026).

Strict information sets: for each candidate entry date we only show filings whose broadcast date is
earlier, fundamentals whose availability date is earlier, and enter at the close of the next session
after the disclosure. Meesho is reported as a prospective case: no forward returns are used to tune
anything (the model's weights were fixed before this module runs, and the walk-forward optimiser
never sees 2026-11 data).
"""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.common import ensure_dir, get_logger, path
from src.investor_classification.classifier import classify

log = get_logger("case_studies")
OUT = ensure_dir(path("outputs") / "case_studies")

CASES = {
    "SAILIFE": {
        "title": "Sai Life Sciences (SAILIFE) — institutional accumulation and TPG block absorption, 2025",
        "segment_index": "Nifty Smallcap 250",
        "entries": [  # (label, disclosure date) -> entry at close of next session
            ("Dec-2024 SHP (original submission 20-Jan-2025)", "2025-01-20"),
            ("Mar-2025 SHP broadcast (SCWF 1.35%->2.62%, Nippon up)", "2025-04-17"),
            ("User's entry window start (Apr-2025, ~Rs 650)", "2025-04-01"),
            ("TPG block absorbed (20-Jun-2025, Rs 722)", "2025-06-20"),
            ("Jun-2025 SHP broadcast (block visible in filing)", "2025-07-18"),
        ],
        "window": ("2024-12-01", "2026-11-13"),
    },
    "ETERNAL": {
        "title": "Zomato / Eternal (ETERNAL) — Motilal Oswal MF block sale absorbed, 16-Jul-2024",
        "segment_index": "Nifty 50",
        "entries": [
            ("Block deal disclosed (16-Jul-2024, Rs 226.85)", "2024-07-16"),
            ("Jun-2024 SHP broadcast (19-Jul-2024)", "2024-07-19"),
            ("Month-end scanner rebalance (31-Jul-2024)", "2024-07-31"),
        ],
        "window": ("2023-06-01", "2026-11-13"),
    },
    "MEESHO": {
        "title": "Meesho (MEESHO) — prospective / live case, November 2026",
        "segment_index": "Nifty Midcap 150",
        "entries": [
            ("Sep-2026 SoftBank block (03-Sep-2026, Rs 206.30)", "2026-09-03"),
            ("Sep-2026 SHP broadcast", "2026-10-05"),
            ("Nov-2026 SoftBank block (13-Nov-2026, Rs 246.75)", "2026-11-13"),
        ],
        "window": ("2025-12-01", "2026-11-13"),
    },
}


def _fmt_pct(x, d=1):
    return "n/a" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x * 100:.{d}f}%"


def entry_returns(px_s: pd.DataFrame, bench: dict[str, pd.Series], disclosure: str, calendar: pd.DatetimeIndex) -> dict:
    d = pd.Timestamp(disclosure)
    i = calendar.searchsorted(d, side="right")  # next session strictly after disclosure
    if i >= len(calendar):
        return {"entry_date": None}
    e = calendar[i]
    s = px_s.set_index("date").tr_index
    if e not in s.index:
        nxt = s.index[s.index >= e]
        if not len(nxt):
            return {"entry_date": None}
        e = nxt[0]
    out = {"entry_date": e.date(), "entry_close": float(px_s.set_index("date").close.get(e, np.nan))}
    for h in (1, 3, 6, 12):
        x = e + pd.DateOffset(months=h)
        if x > calendar[-1]:
            out[f"ret_{h}m"] = np.nan
            for bn in bench:
                out[f"{bn}_{h}m"] = np.nan
            continue
        xs = s.index[s.index <= x][-1]
        out[f"ret_{h}m"] = s[xs] / s[e] - 1
        for bn, b in bench.items():
            bb = b.reindex(b.index.union([e, xs])).ffill()
            out[f"{bn}_{h}m"] = bb[xs] / bb[e] - 1
    xs = s.index[-1]
    out["ret_to_date"] = s[xs] / s[e] - 1
    out["to_date"] = xs.date()
    for bn, b in bench.items():
        bb = b.reindex(b.index.union([e, xs])).ffill()
        out[f"{bn}_to_date"] = bb[xs] / bb[e] - 1
    return out


def decomposition(sym: str, e: pd.Timestamp, x: pd.Timestamp, px_s: pd.DataFrame, af: pd.DataFrame,
                  shares: pd.DataFrame) -> dict:
    """log price return = log earnings growth + log multiple change - log share-count change
    (P/E basis when earnings > 0 at both ends; else P/S basis). Uses only FY data available at each date."""
    a = af[af.symbol == sym].sort_values("available_date")
    s = px_s.set_index("date")
    def at(d):
        f = a[a.available_date <= d].tail(1)
        sh = shares[(shares.symbol == sym) & (shares.effective_date <= d)].tail(1)
        return (f.iloc[0] if len(f) else None), (float(sh.total_shares.iloc[0]) if len(sh) else np.nan)
    fe, she = at(e)
    fx, shx = at(x)
    if fe is None or fx is None:
        return {"note": "fundamentals not available at both dates"}
    pe_, px_ = s.close.asof(e), s.close.asof(x)
    adj_ratio = s.adj_close.asof(x) / s.adj_close.asof(e)
    out = {"entry_fy": str(pd.Timestamp(fe.fy_end).date()), "exit_fy": str(pd.Timestamp(fx.fy_end).date()),
           "price_return": adj_ratio - 1}
    mc_e, mc_x = pe_ * she, px_ * shx
    basis = "pat" if (fe.pat > 0 and fx.pat > 0) else "revenue"
    E0, E1 = fe[basis], fx[basis]
    out.update({"basis": "P/E" if basis == "pat" else "P/S", "fund_entry_cr": E0 / 1e7, "fund_exit_cr": E1 / 1e7,
                "multiple_entry": mc_e / E0, "multiple_exit": mc_x / E1})
    lr = np.log(adj_ratio)
    g = np.log(E1 / E0)
    m = np.log((mc_x / E1) / (mc_e / E0))
    dil = np.log(shx / she) if np.isfinite(shx / she) else 0.0
    out.update({"log_return": lr, "log_fund_growth": g, "log_multiple_change": m, "log_share_dilution": dil,
                "share_of_return_from_fundamentals": g / lr if lr else np.nan,
                "share_of_return_from_multiple": m / lr if lr else np.nan})
    return out


def run(panel: pd.DataFrame, px: pd.DataFrame, idx: pd.DataFrame) -> pd.DataFrame:
    proc = path("processed")
    sp = pd.read_parquet(proc / "shareholding_filings_pit.parquet")
    q = pd.read_parquet(proc / "shareholding_quarterly.parquet")
    fam = pd.read_parquet(proc / "named_holder_families.parquet")
    af = pd.read_parquet(proc / "accounting_features.parquet") if (proc / "accounting_features.parquet").exists() else pd.DataFrame()
    ear = pd.read_parquet(proc / "earnings_announcement_returns.parquet")
    events = pd.read_csv(path("reference") / "case_study_events.csv")
    calendar = pd.DatetimeIndex(np.sort(px.date.unique()))
    bench = {n: idx[idx["index"] == n].set_index("date").tri for n in idx["index"].unique()}
    summary_rows = []
    for sym, cfg in CASES.items():
        px_s = px[px.symbol == sym].sort_values("date")
        if px_s.empty:
            log.warning("no prices for %s", sym)
            continue
        bsel = {"Nifty 500": bench["Nifty 500"], cfg["segment_index"]: bench.get(cfg["segment_index"], bench["Nifty 500"])}
        lines = [f"# {cfg['title']}", "",
                 "_All figures computed from NSE archive data in this repository unless marked as news/user-provided. "
                 "Entry = close of the first NSE session strictly after the disclosure. Returns are total returns "
                 "(dividends on ex-date) vs approximate TRI benchmarks._", ""]
        # ---- events
        ev = events[events.case == sym]
        lines += ["## 1. Event timeline and verification", "",
                  "| date | type | description | price (Rs) | verification | buyer classification (rule-based) |", "|---|---|---|---|---|---|"]
        for r in ev.itertuples(index=False):
            cls = ""
            if isinstance(r.buyers_reported, str) and r.event_type == "block_deal":
                parts = [b.strip() for b in r.buyers_reported.replace("reports conflict:", "").split(";")]
                cl = []
                for b in parts:
                    nm = b.split("(")[0].strip()
                    if nm:
                        c = classify(nm)
                        cl.append(f"{nm}: {c['category']} ({c['confidence']})")
                cls = "<br>".join(cl)
            lines.append(f"| {r.event_date} | {r.event_type} | {r.description} | {'' if pd.isna(r.price_inr) else r.price_inr} | "
                         f"{r.verification_status} | {cls} |")
        # block-day footprint in exchange data
        lines += ["", "**Exchange footprint of block days (NSE bhavcopy + delivery):**", "",
                  "| date | close | day return | volume / 126d median | delivery % | traded value (Rs cr) |", "|---|---|---|---|---|---|"]
        pv = px_s.set_index("date")
        medv = pv.volume.rolling(126, min_periods=20).median().shift(1)
        for r in ev[ev.event_type == "block_deal"].itertuples(index=False):
            d = pd.Timestamp(r.event_date)
            if d in pv.index:
                lines.append(f"| {d.date()} | {pv.close[d]:.2f} | {_fmt_pct(pv.ret_tr[d])} | {pv.volume[d] / medv[d]:.1f}x | "
                             f"{pv.deliv_pct[d]:.1f} | {pv.value_cr[d]:,.0f} |")
        # ---- ownership
        qs = q[q.symbol == sym].sort_values("period_end")
        lines += ["", "## 2. Quarterly ownership (NSE SHP XBRL, point-in-time)", "",
                  "| quarter | available (broadcast) | promoter % | FII (FPI) % | MF % | insurance % | total inst % | Δ FII pp | Δ MF pp | Δ inst pp | share-count chg (ex-split) |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in qs.itertuples(index=False):
            lines.append(f"| {r.period_end.date()} | {pd.Timestamp(r.avail_date).date()} | {r.promoter_pct:.2f} | {r.fii_pct:.2f} | "
                         f"{r.mf_pct:.2f} | {r.insurance_pct:.2f} | {r.inst_pct:.2f} | {r.d_fii_1q:+.2f} | {r.d_mf_1q:+.2f} | "
                         f"{r.d_inst_1q:+.2f} | {_fmt_pct(r.share_change_ex_split, 2)} |")
        # named holders
        fs = fam[(fam.symbol == sym) & pd.to_datetime(fam.period_end).dt.is_quarter_end]
        f = fs.pivot_table(index=["family", "category"], columns="period_end", values="pct", aggfunc="sum")
        if not f.empty:
            f = f.reindex(sorted(f.columns), axis=1)
            f.columns = [c.strftime("%Y-%m") for c in f.columns]
            f = f[[c for c in f.columns]].fillna(0).round(2)
            f = f[(f.max(axis=1) >= 1.0)]
            f = f.loc[~f.index.get_level_values("category").isin(["promoter_group"])]
            lines += ["", "**Named holders >1% (family level, excl. promoter group), % of equity:**", "",
                      f.reset_index().to_markdown(index=False)]
        # ---- fundamentals available
        if len(af):
            a = af[af.symbol == sym].sort_values("fy_end")
            if len(a):
                lines += ["", "## 3. Fundamentals and when they became public (Yahoo annual data — supplementary; availability from NSE board-meeting dates)", "",
                          "| FY end | available from | revenue (Rs cr) | EBITDA (Rs cr) | PAT (Rs cr) | rev growth | EBITDA margin | PAT growth | ROCE |",
                          "|---|---|---|---|---|---|---|---|---|"]
                for r in a.itertuples(index=False):
                    lines.append(f"| {r.fy_end.date()} | {r.available_date.date()} | {r.revenue / 1e7:,.0f} | {r.ebitda / 1e7:,.0f} | "
                                 f"{r.pat / 1e7:,.0f} | {_fmt_pct(r.rev_g)} | {_fmt_pct(r.ebitda_margin)} | {_fmt_pct(r.pat_g)} | {_fmt_pct(r.roce)} |")
        e_s = ear[ear.symbol == sym].sort_values("d0")
        if len(e_s):
            lines += ["", "**Results-announcement reactions (3-session abnormal return vs Nifty 500):**", "",
                      "| board meeting | reaction |", "|---|---|"]
            for r in e_s.itertuples(index=False):
                lines.append(f"| {r.bm_date.date()} | {_fmt_pct(r.ear)} |")
        # ---- entries
        lines += ["", "## 4. Forward returns from each disclosure (entry at next session close)", "",
                  "| trigger | entry date | entry close | 1m | 3m | 6m | 12m | to date | Nifty 500 to date | segment index to date |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        ent_rows = []
        for lab, dd in cfg["entries"]:
            er = entry_returns(px_s, bsel, dd, calendar)
            if er.get("entry_date") is None:
                continue
            ent_rows.append({"case": sym, "trigger": lab, **er})
            seg = cfg["segment_index"]
            def rel(h):
                v, b = er.get(f"ret_{h}m"), er.get(f"Nifty 500_{h}m")
                return "n/a" if v is None or not np.isfinite(v) else f"{v * 100:+.1f}% ({(v - b) * 100:+.1f} vs N500)"
            lines.append(f"| {lab} | {er['entry_date']} | {er['entry_close']:.2f} | {rel(1)} | {rel(3)} | {rel(6)} | {rel(12)} | "
                         f"{_fmt_pct(er['ret_to_date'])} | {_fmt_pct(er['Nifty 500_to_date'])} | {_fmt_pct(er.get(seg + '_to_date'))} |")
        summary_rows += ent_rows
        # ---- decomposition
        if len(af) and ent_rows:
            lines += ["", "## 5. Return decomposition: earnings growth vs re-rating", "",
                      "log(price return) = log(fundamental growth) + log(multiple change) − log(share-count growth); "
                      "fundamentals = latest FY figures public at each date.", "",
                      "| entry | horizon end | basis | FY at entry→exit | fundamental entry→exit (Rs cr) | multiple entry→exit | price return | from fundamentals | from multiple |",
                      "|---|---|---|---|---|---|---|---|---|"]
            for er in ent_rows:
                e = pd.Timestamp(er["entry_date"])
                for h in (12, None):
                    x = e + pd.DateOffset(months=12) if h else pd.Timestamp(er["to_date"])
                    if x > calendar[-1]:
                        continue
                    dcp = decomposition(sym, e, x, px_s, af, sp.rename(columns={}))
                    if "note" in dcp:
                        continue
                    lines.append(f"| {e.date()} | {x.date()} | {dcp['basis']} | {dcp['entry_fy'][:7]}→{dcp['exit_fy'][:7]} | "
                                 f"{dcp['fund_entry_cr']:,.0f}→{dcp['fund_exit_cr']:,.0f} | {dcp['multiple_entry']:.1f}→{dcp['multiple_exit']:.1f} | "
                                 f"{_fmt_pct(dcp['price_return'])} | {dcp['log_fund_growth']:+.2f} log pts | {dcp['log_multiple_change']:+.2f} log pts |")
        # ---- scanner view
        ps = panel[panel.symbol == sym].sort_values("date")
        ps = ps[(ps.date >= pd.Timestamp(cfg["window"][0]))]
        if len(ps):
            cols = ["date", "eligible", "segment", "mcap_rank", "score_B_shareholding", "score_A_block", "score_earnings",
                    "score_D_inst_block_earnings", "score_E_full", "rank_D", "rank_E", "d_fii_1q", "d_mf_1q", "d_inst_1q",
                    "nh_credible_increase_pp", "absorption_event", "ear"]
            ps = ps.copy()
            ps["rank_D"] = ps.apply(lambda r: _rank(panel, r.date, sym, "score_D_inst_block_earnings"), axis=1)
            ps["rank_E"] = ps.apply(lambda r: _rank(panel, r.date, sym, "score_E_full"), axis=1)
            t = ps[[c for c in cols if c in ps.columns]].copy()
            t["date"] = t.date.dt.date
            lines += ["", "## 6. What the scanner saw at each month-end (scores 0-100; rank among eligible stocks)", "",
                      t.round(2).to_markdown(index=False)]
        _chart(sym, cfg, px_s, bsel, ev)
        lines += ["", f"![price chart]({sym}_chart.png)", ""]
        (OUT / f"{sym}_case_study.md").write_text("\n".join(lines))
    summ = pd.DataFrame(summary_rows)
    summ.to_csv(OUT / "case_study_entry_returns.csv", index=False)
    return summ


def _rank(panel, d, sym, col):
    g = panel[(panel.date == d) & panel.eligible & panel[col].notna()]
    if sym not in set(g.symbol):
        return np.nan
    return int(g[col].rank(ascending=False, method="min")[g.symbol == sym].iloc[0])


def _chart(sym, cfg, px_s, bsel, ev):
    s = px_s.set_index("date")
    a, b = pd.Timestamp(cfg["window"][0]), pd.Timestamp(cfg["window"][1])
    s = s[(s.index >= a) & (s.index <= b)]
    if s.empty:
        return
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    base = s.tr_index.iloc[0]
    ax.plot(s.index, s.tr_index / base * 100, color="#1f5fa8", lw=1.6, label=f"{sym} (total return)")
    for i, (bn, bs) in enumerate(bsel.items()):
        bb = bs.reindex(s.index).ffill()
        ax.plot(s.index, bb / bb.dropna().iloc[0] * 100, color=["black", "#888"][i], ls=["--", ":"][i], lw=1.1, label=bn + " TRI (approx.)")
    for r in ev.itertuples(index=False):
        d = pd.Timestamp(r.event_date)
        if a <= d <= b:
            ax.axvline(d, color="#d9822b", lw=0.8, alpha=0.8)
            ax.text(d, ax.get_ylim()[1], r.event_type.replace("_", " "), rotation=90, fontsize=7, va="top", ha="right", color="#d9822b")
    ax.legend(fontsize=8, frameon=False)
    ax.set_title(cfg["title"], fontsize=10, loc="left", fontweight="bold")
    ax.grid(alpha=0.25)
    ax2.bar(s.index, s.value_cr, color="#7a7a7a", width=1.0)
    ax2.set_ylabel("traded value\n(Rs cr)", fontsize=8)
    ax2.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(OUT / f"{sym}_chart.png", dpi=130)
    plt.close(fig)
