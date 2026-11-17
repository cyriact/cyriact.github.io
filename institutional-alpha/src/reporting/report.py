"""Data-source / quality appendix and universe-coverage diagnostics."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from src.common import ensure_dir, path

TAB = ensure_dir(path("outputs") / "tables")


def universe_coverage() -> pd.DataFrame:
    """Per month-end: eligible counts by segment and how much of the PIT Nifty 500 we can score."""
    p = pd.read_parquet(path("processed") / "monthly_panel.parquet",
                        columns=["date", "symbol", "eligible", "segment", "mcap_cr", "d_inst_1q", "adtv_63_cr", "traded_today"])
    mem = pd.read_csv(path("raw") / "external" / "nse-historical-membership" / "index_history" / "data" / "index_membership_history.csv")
    n5 = mem[mem.index_name == "Nifty 500"].copy()
    n5["valid_from"] = pd.to_datetime(n5.valid_from)
    n5["valid_to"] = pd.to_datetime(n5.valid_to)
    rows = []
    for d, g in p.groupby("date"):
        members = set(n5[(n5.valid_from <= d) & (n5.valid_to.isna() | (n5.valid_to > d))].symbol)
        el = g[g.eligible]
        have_mcap = set(g[g.mcap_cr.notna()].symbol)
        traded = set(g[g.traded_today == True].symbol)  # noqa: E712
        rows.append({"date": d, "n_eligible": len(el), "n_large": int((el.segment == "large").sum()),
                     "n_mid": int((el.segment == "mid").sum()), "n_small": int((el.segment == "small").sum()),
                     "n_with_inst_signal": int(el.d_inst_1q.notna().sum()),
                     "nifty500_members_pit": len(members),
                     "nifty500_members_traded": len(members & traded),
                     "nifty500_members_with_shp": len(members & have_mcap),
                     "nifty500_coverage": len(members & have_mcap) / max(1, len(members & traded)),
                     "median_adtv_eligible_cr": el.adtv_63_cr.median()})
    cov = pd.DataFrame(rows)
    cov.to_csv(TAB / "universe_coverage.csv", index=False)
    return cov


def build_data_appendix() -> None:
    cat = json.loads((path("metadata") / "datasets.json").read_text())
    cov = universe_coverage()
    val = validation_summary()
    lines = ["# Data sources, coverage and quality", "",
             "Generated from `data/metadata/datasets.json` by `src/reporting/report.py`.", "",
             "| dataset | source | coverage | records | quality score | known limitations |", "|---|---|---|---|---|---|"]
    for k, v in sorted(cat.items()):
        lines.append(f"| {k} | {v.get('source_url', '')} | {v.get('coverage_start', '')} → {v.get('coverage_end', '')} | "
                     f"{v.get('n_records', '')} | {v.get('quality_score', '')} | {v.get('limitations', '')} |")
    lines += ["", "## Validation checks", "", val.to_markdown(index=False)]
    lines += ["", "## Universe coverage (month-end snapshots)", "",
              f"- Months: {len(cov)}; eligible stocks per month: median {cov.n_eligible.median():.0f} "
              f"(min {cov.n_eligible.min()}, max {cov.n_eligible.max()}).",
              f"- Share of point-in-time Nifty 500 constituents (that traded) with shareholding coverage: median "
              f"{cov.nifty500_coverage.median():.1%}, min {cov.nifty500_coverage.min():.1%}.",
              f"- Eligible stocks with a fresh institutional-change signal: median {cov.n_with_inst_signal.median():.0f}.", "",
              "Retrieval dates are recorded per dataset in `data/metadata/datasets.json`."]
    (ensure_dir(path("outputs") / "reports") / "Data_Sources_and_Quality.md").write_text("\n".join(lines))


def validation_summary() -> pd.DataFrame:
    """Data-validation checks recorded for the report (each row: check, result, detail)."""
    proc, raw = path("processed"), path("raw")
    rows = []
    px = pd.read_parquet(proc / "prices_daily.parquet",
                         columns=["symbol", "date", "prior_close", "prevclose", "dps", "ca_flag", "ca_inferred",
                                  "demerger_adj", "ret_px", "value_cr", "deliv_pct"])
    cal = pd.Series(np.sort(px.date.unique()))
    per_year = cal.groupby(cal.dt.year).size()
    full = per_year[per_year.index < cal.max().year]
    rows.append(("Trading sessions captured", f"{len(cal):,}", f"{cal.min().date()} to {cal.max().date()}; "
                 f"{full.min()}-{full.max()} per full calendar year"))
    ca = pd.read_parquet(proc / "corporate_actions.parquet")
    d = ca[ca.dps > 0].merge(px[["symbol", "date", "prior_close", "prevclose"]], left_on=["symbol", "ex_date"],
                             right_on=["symbol", "date"])
    d = d[(d.dps / d.prior_close) > 0.02]
    same = ((d.prevclose - d.prior_close).abs() < 0.005 * d.prior_close).mean()
    rows.append(("NSE PREVCLOSE adjusted for dividends?", "No", f"{same:.1%} of {len(d):,} ex-dates with yield >2% have PREVCLOSE = prior close -> dividends added explicitly"))
    rows.append(("Split/bonus/consolidation factors applied", f"{int((px.ca_flag & ~px.ca_inferred).sum()):,}",
                 "from NSE corporate-action records, validated against the ex-date price jump"))
    rows.append(("Split-like jumps inferred (no record)", f"{int((px.ca_flag & px.ca_inferred).sum()):,}", "flagged ca_inferred"))
    rows.append(("Demerger ex-dates neutralised", f"{int(px.demerger_adj.sum()):,}", "ex-date return set to 0 (value distributed)"))
    rows.append(("Dividend events credited", f"{int((px.dps > 0).sum()):,}", "cash dividends on ex-date"))
    big = px[(px.ret_px.abs() > 0.5) & (px.value_cr > 1)]
    rows.append(("Remaining |daily return| > 50% with traded value > Rs 1 cr", f"{len(big)}",
                 ", ".join(f"{s} {d.date()}" for s, d in big[["symbol", "date"]].head(8).itertuples(index=False))))
    rows.append(("Delivery data coverage", f"{px.deliv_pct.notna().mean():.1%}", "share of price rows with MTO delivery %"))
    sp = pd.read_parquet(proc / "shareholding_filings_pit.parquet")
    ref = pd.read_csv(raw / "external" / "nse-historical-membership" / "shareholding_history" / "data" / "parsed" / "_flat.csv")
    m = sp.merge(ref[["source_filename", "promoter_pct", "fii_pct", "dii_pct"]].rename(
        columns={"source_filename": "filename", "promoter_pct": "p_ref", "fii_pct": "f_ref", "dii_pct": "d_ref"}), on="filename")
    ok_p = ((m.promoter_pct - m.p_ref).abs() < 0.05)[m.p_ref.notna()].mean()
    rows.append(("SHP parser vs external repo parse (promoter %)", f"{ok_p:.2%} within 0.05pp", f"{m.p_ref.notna().sum():,} overlapping filings"))
    ok_f = ((m.inst_reported_pct - (m.f_ref + m.d_ref)).abs() < 0.05)[m.f_ref.notna() & m.d_ref.notna()].mean() \
        if "inst_reported_pct" in m else np.nan
    rows.append(("SHP parser vs external repo (reported FII+DII %)", f"{ok_f:.2%} within 0.05pp", "2022+ filings where the repo has FII/DII"))
    vj = json.loads((raw / "external" / "nse-historical-membership" / "shareholding_history" / "data" / "validation_vs_stockedge.json").read_text())
    rows.append(("External repo's own check vs StockEdge (±0.5pp)", ", ".join(f"{k} {v['rate_pct']}%" for k, v in vj["summary"].items()),
                 f"{vj['n_overlap_keys']:,} symbol-quarters"))
    mem = pd.read_csv(raw / "external" / "nse-historical-membership" / "index_history" / "data" / "index_membership_history.csv")
    cur = set(mem[(mem.index_name == "Nifty 500") & mem.valid_to.isna()].symbol)
    nse = set(pd.read_csv(raw / "reference" / "ind_nifty500list.csv").Symbol)
    rows.append(("PIT index membership: current Nifty 500 vs NSE list", f"{len(cur & nse)}/{len(nse)} match",
                 f"only in NSE list: {sorted(nse - cur)}; only in repo: {sorted(cur - nse)}"))
    sh = pd.read_parquet(proc / "shp_filings.parquet")
    rows.append(("SHP XBRL filings parsed", f"{sh.total_shares.notna().sum():,}", f"{sh.symbol.nunique():,} symbols; 72 URLs returned 404 on the archive"))
    out = pd.DataFrame(rows, columns=["check", "result", "detail"])
    out.to_csv(TAB / "data_validation.csv", index=False)
    return out
