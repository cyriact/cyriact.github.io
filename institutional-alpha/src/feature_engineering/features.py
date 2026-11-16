"""Point-in-time monthly feature panel.

Timing convention (no look-ahead):
  * Signal date t  = last NSE session of each month. Every feature uses data available at the
    close of t: prices/volumes up to t, SHP filings whose `effective_date` (first session strictly
    after public broadcast) is <= t, results whose announcement window closed by t, fundamentals
    whose imputed availability date is <= t.
  * Execution  = close of the first session after t (t+1). Forward returns run from close(t+1) to
    close of the session after the signal date h months later. The institutional transaction price
    is never used as an entry price.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.common import SETTINGS, ensure_dir, get_logger, path, register_dataset

log = get_logger("features")
U = SETTINGS["universe"]


# ----------------------------------------------------------------------------- helpers
def wide(px: pd.DataFrame, col: str) -> pd.DataFrame:
    return px.pivot(index="date", columns="symbol", values=col).sort_index()


def month_end_sessions(calendar: pd.DatetimeIndex, start: str, end: str) -> pd.DatetimeIndex:
    s = pd.Series(calendar, index=calendar)
    me = s.groupby([calendar.year, calendar.month]).max()
    me = pd.DatetimeIndex(me.values)
    return me[(me >= pd.Timestamp(start)) & (me <= pd.Timestamp(end))]


def next_sessions(dates: pd.DatetimeIndex, calendar: pd.DatetimeIndex, k: int = 1) -> pd.DatetimeIndex:
    """k-th session strictly after each date; NaT when beyond the data (latest live signal date)."""
    idx = calendar.searchsorted(dates, side="right") + (k - 1)
    out = calendar[np.clip(idx, 0, len(calendar) - 1)]
    return pd.DatetimeIndex(np.where(idx < len(calendar), out, np.datetime64("NaT")))


# ----------------------------------------------------------------------------- price / volume
def price_features(px: pd.DataFrame, bench_tr: pd.Series, dates: pd.DatetimeIndex) -> pd.DataFrame:
    close = wide(px, "close")
    adj = wide(px, "adj_close")
    tr = wide(px, "tr_index")
    vol = wide(px, "volume")
    val = wide(px, "value_cr")
    deliv = wide(px, "deliv_pct")
    dps = wide(px, "dps").fillna(0.0)
    ret = adj.pct_change(fill_method=None)
    bench = bench_tr.reindex(adj.index).ffill()

    feats = {}
    feats["hist_days"] = adj.notna().cumsum()
    feats["traded_today"] = close.notna()
    a_ff = adj.ffill(limit=10)
    feats["mom_12_1"] = a_ff.shift(21) / a_ff.shift(252) - 1
    feats["mom_6_1"] = a_ff.shift(21) / a_ff.shift(126) - 1
    feats["ret_1m"] = a_ff / a_ff.shift(21) - 1
    feats["ret_3m"] = a_ff / a_ff.shift(63) - 1
    feats["rs_6m"] = (a_ff / a_ff.shift(126)).div(bench / bench.shift(126), axis=0) - 1
    feats["rs_12m"] = (a_ff / a_ff.shift(252)).div(bench / bench.shift(252), axis=0) - 1
    feats["rev_36m"] = a_ff / a_ff.shift(756) - 1
    feats["vol_1y"] = ret.rolling(252, min_periods=120).std() * np.sqrt(252)
    ma200 = a_ff.rolling(200, min_periods=150).mean()
    ma50 = a_ff.rolling(50, min_periods=40).mean()
    feats["px_vs_ma200"] = a_ff / ma200 - 1
    feats["ma50_gt_ma200"] = (ma50 > ma200).astype(float).where(ma200.notna())
    feats["dd_52w"] = a_ff / a_ff.rolling(252, min_periods=120).max() - 1
    feats["adtv_63_cr"] = val.rolling(63, min_periods=40).median()
    feats["deliv_63"] = deliv.rolling(63, min_periods=30).mean()
    feats["deliv_chg"] = feats["deliv_63"] - deliv.rolling(252, min_periods=120).mean()
    up = (ret > 0)
    upv = vol.where(up, 0).rolling(63, min_periods=40).sum()
    dnv = vol.where(~up & ret.notna(), 0).rolling(63, min_periods=40).sum()
    feats["updown_vol_63"] = np.log((upv + 1) / (dnv + 1))
    medv = vol.rolling(126, min_periods=60).median().shift(1)
    shock = (vol >= 5 * medv) & (deliv >= 40) & (ret >= -0.01)
    feats["absorb_days_63"] = shock.astype(float).rolling(63, min_periods=20).sum()
    feats["absorb_value_63"] = val.where(shock, 0).rolling(63, min_periods=20).sum() / feats["adtv_63_cr"]
    sell_shock = (vol >= 5 * medv) & (ret < -0.03)
    feats["distrib_days_63"] = sell_shock.astype(float).rolling(63, min_periods=20).sum()
    feats["div_yield_ttm"] = dps.rolling(252, min_periods=1).sum() / close.ffill(limit=10)
    feats["div_paid_3y"] = (dps.rolling(252, min_periods=1).sum() > 0).astype(float) + \
                           (dps.shift(252).rolling(252, min_periods=1).sum() > 0).astype(float) + \
                           (dps.shift(504).rolling(252, min_periods=1).sum() > 0).astype(float)
    feats["close"] = close.ffill(limit=10)
    feats["adj_close"] = a_ff
    feats["tr_index"] = tr.ffill(limit=10)

    out = []
    for name, f in feats.items():
        s = f.reindex(dates).stack(future_stack=True).rename(name)
        out.append(s)
    df = pd.concat(out, axis=1).reset_index().rename(columns={"level_0": "date", "level_1": "symbol"})
    df = df[df["hist_days"] > 0]
    return df


# ----------------------------------------------------------------------------- earnings proxy
def earnings_announcement_returns(px: pd.DataFrame, bench_ret: pd.Series, bm: pd.DataFrame,
                                  calendar: pd.DatetimeIndex) -> pd.DataFrame:
    """3-session abnormal return around results board meetings (close d0-1 -> close d0+1)."""
    bm = bm[bm.is_results].copy()
    bm = bm[bm.bm_date <= calendar[-1]]
    i0 = calendar.searchsorted(bm.bm_date.values, side="left")
    ok = (i0 >= 1) & (i0 + 1 < len(calendar))
    bm = bm[ok]
    i0 = i0[ok]
    bm["d0"] = calendar[i0]
    bm["d_start"] = calendar[i0 - 1]
    bm["d_end"] = calendar[i0 + 1]
    tr = px.pivot(index="date", columns="symbol", values="tr_index").sort_index().ffill(limit=5)
    b = (1 + bench_ret.reindex(calendar).fillna(0)).cumprod()
    out = []
    cols = set(tr.columns)
    for r in bm.itertuples(index=False):
        if r.symbol not in cols:
            continue
        s0, s1 = tr.at[r.d_start, r.symbol], tr.at[r.d_end, r.symbol]
        if not (np.isfinite(s0) and np.isfinite(s1)):
            continue
        rb = b.at[r.d_end] / b.at[r.d_start] - 1
        out.append((r.symbol, r.bm_date, r.d0, r.d_end, s1 / s0 - 1 - rb))
    ear = pd.DataFrame(out, columns=["symbol", "bm_date", "d0", "available_date", "ear"])
    ear = ear.sort_values(["symbol", "available_date"]).drop_duplicates(["symbol", "d0"])
    ear["ear_prev"] = ear.groupby("symbol").ear.shift(1)
    ear["ear_sum2"] = ear.ear + ear.ear_prev
    ear["ear_pos_4"] = ear.groupby("symbol").ear.transform(lambda s: (s > 0).astype(float).rolling(4, min_periods=2).mean())
    ear["ear_accel"] = ear.ear - ear.ear_prev
    return ear


# ----------------------------------------------------------------------------- fundamentals (Yahoo)
def accounting_features(fund: pd.DataFrame, bm: pd.DataFrame) -> pd.DataFrame:
    """Annual statements -> growth/margin/quality metrics with imputed availability dates.

    Availability of a fiscal year ending FYE: first NSE results board meeting dated within
    [FYE+15d, FYE+90d]; if none found, FYE+60d (SEBI LODR deadline for annual results) + 1 day.
    """
    a = fund[fund.freq == "annual"].pivot_table(index=["symbol", "period_end"], columns="field", values="value")
    a = a.reset_index().sort_values(["symbol", "period_end"])
    def col(c, alt=None):
        if c in a.columns:
            return a[c] if alt is None or alt not in a.columns else a[c].fillna(a[alt])
        return a[alt] if alt in a.columns else pd.Series(np.nan, index=a.index)
    rev = col("TotalRevenue", "OperatingRevenue")
    ebitda = col("EBITDA", "NormalizedEBITDA")
    ebit = col("EBIT", "OperatingIncome")
    ni = col("NetIncomeCommonStockholders", "NetIncome")
    ocf = col("OperatingCashFlow")
    capex = col("CapitalExpenditure")
    debt = col("TotalDebt").fillna(0)
    cash = col("CashCashEquivalentsAndShortTermInvestments", "CashAndCashEquivalents").fillna(0)
    eq = col("StockholdersEquity")
    f = pd.DataFrame({"symbol": a.symbol, "fy_end": a.period_end, "revenue": rev, "ebitda": ebitda, "ebit": ebit,
                      "pat": ni, "ocf": ocf, "capex": capex, "fcf": ocf + capex.fillna(0), "debt": debt,
                      "cash": cash, "equity": eq})
    g = f.groupby("symbol")
    f["rev_g"] = f.revenue / g.revenue.shift(1) - 1
    f["ebitda_g"] = (f.ebitda - g.ebitda.shift(1)) / g.ebitda.shift(1).abs()
    f["pat_g"] = (f.pat - g.pat.shift(1)) / g.pat.shift(1).abs()
    f["ebitda_margin"] = f.ebitda / f.revenue
    f["margin_chg"] = f.ebitda_margin - g.ebitda_margin.shift(1)
    f["rev_accel"] = f.rev_g - f.groupby("symbol").rev_g.shift(1)
    f["ebitda_vs_rev_g"] = f.ebitda_g - f.rev_g
    f["ocf_conv"] = f.ocf / f.ebitda.where(f.ebitda > 0)
    cap_emp = (f.equity + f.debt - f.cash)
    f["roce"] = f.ebit / cap_emp.where(cap_emp > 0)
    f["debt_to_equity"] = f.debt / f.equity.where(f.equity > 0)
    f["net_debt_ebitda"] = (f.debt - f.cash) / f.ebitda.where(f.ebitda > 0)
    f["turnaround"] = ((g.pat.shift(1) <= 0) & (f.pat > 0)).astype(float)
    f.loc[g.pat.shift(1).isna(), "turnaround"] = np.nan
    # availability
    res_bm = bm[bm.is_results][["symbol", "bm_date"]]
    f["available_date"] = f.fy_end + pd.Timedelta(days=61)
    mm = f[["symbol", "fy_end"]].reset_index().merge(res_bm, on="symbol", how="left")
    mm = mm[(mm.bm_date >= mm.fy_end + pd.Timedelta(days=15)) & (mm.bm_date <= mm.fy_end + pd.Timedelta(days=90))]
    first_bm = mm.groupby("index").bm_date.min()
    f.loc[first_bm.index, "available_date"] = first_bm + pd.Timedelta(days=1)
    return f


# ----------------------------------------------------------------------------- assembly
def asof_merge(panel: pd.DataFrame, events: pd.DataFrame, on_date: str, cols: list[str], prefix: str = "") -> pd.DataFrame:
    ev = events[["symbol", on_date] + cols].dropna(subset=[on_date]).sort_values(on_date)
    ev = ev.rename(columns={c: prefix + c for c in cols})
    p = panel.sort_values("date")
    m = pd.merge_asof(p, ev, left_on="date", right_on=on_date, by="symbol", direction="backward")
    return m.drop(columns=[on_date])


def build_panel(px: pd.DataFrame, idx: pd.DataFrame, q: pd.DataFrame, nhf: pd.DataFrame, bm: pd.DataFrame,
                fund: pd.DataFrame | None, sectors: pd.DataFrame) -> pd.DataFrame:
    calendar = pd.DatetimeIndex(np.sort(px.date.unique()))
    bt = SETTINGS["backtest"]
    dates = month_end_sessions(calendar, "2017-12-01", calendar[-1].strftime("%Y-%m-%d"))
    n500 = idx[idx["index"] == "Nifty 500"].set_index("date")
    bench_tr = n500.tri
    bench_ret = n500.ret_tr

    log.info("price features for %d month-ends", len(dates))
    pf = price_features(px, bench_tr, dates)

    # ---- shares outstanding / market cap (latest effective filing, split-adjusted) -----------
    sp = pd.read_parquet(path("processed") / "shareholding_filings_pit.parquet")
    sh = sp[["symbol", "effective_date", "period_end", "total_shares"]].dropna()
    pf = asof_merge(pf, sh.rename(columns={"period_end": "shp_period_end"}), "effective_date",
                    ["shp_period_end", "total_shares"])
    # adjust share count for splits/bonuses/consolidations after the SHP period end:
    # k = cumulative product of share factors (ca_ratio; demerger neutralisation excluded)
    adjr = px[["symbol", "date", "ca_ratio"]].copy()
    adjr["k"] = adjr.ca_ratio.fillna(1.0).groupby(adjr.symbol, sort=False).cumprod()
    k_at = adjr.set_index(["symbol", "date"]).k
    k_now = pd.Series(k_at.reindex(list(zip(pf.symbol, pf.date))).values, index=pf.index)
    pe_dates = pf.shp_period_end
    calendar_idx = calendar.searchsorted(pe_dates.fillna(calendar[0]).values, side="right") - 1
    pe_sess = calendar[np.clip(calendar_idx, 0, len(calendar) - 1)]
    k_then = pd.Series(k_at.reindex(list(zip(pf.symbol, pe_sess))).values, index=pf.index)
    # shares now = shares filed at period end x share factors applied after it
    pf["shares_adj"] = pf.total_shares * (k_now / k_then).fillna(1.0)
    pf["mcap_cr"] = pf.close * pf.shares_adj / 1e7

    # ---- shareholding signals (latest quarter-end filing effective by t) ------------------------
    qcols = [c for c in q.columns if c.startswith(("d_", "flow_"))] + [
        "inst_acc_streak", "mf_acc_streak", "fii_acc_streak", "inst_pct", "mf_pct", "fii_pct", "promoter_pct",
        "dom_inst_pct", "insurance_pct", "free_float_pct", "dilution_flag", "share_change_ex_split", "period_end",
        "avail_date"]
    qq = q[["symbol", "effective_date"] + qcols].rename(columns={"period_end": "sig_period_end", "avail_date": "sig_avail_date"})
    pf = asof_merge(pf, qq, "effective_date", [c if c not in ("period_end", "avail_date") else
                                               {"period_end": "sig_period_end", "avail_date": "sig_avail_date"}[c] for c in qcols])
    pf["sig_age_days"] = (pf.date - pf.sig_avail_date).dt.days
    stale = pf.sig_age_days > 200  # older than ~2 quarters -> do not use
    for c in [c for c in pf.columns if c.startswith(("d_", "flow_"))] + ["inst_acc_streak", "mf_acc_streak", "fii_acc_streak"]:
        pf.loc[stale, c] = np.nan
    nh_cols = [c for c in nhf.columns if c.startswith("nh_")] + ["absorption_event", "absorption_ratio"]
    nh = nhf.merge(q[["symbol", "period_end", "effective_date"]], on=["symbol", "period_end"], how="left")
    pf = asof_merge(pf, nh[["symbol", "effective_date"] + nh_cols], "effective_date", nh_cols)
    pf.loc[stale, nh_cols] = np.nan

    # ---- earnings proxy ----------------------------------------------------------------------------
    ear = earnings_announcement_returns(px, bench_ret, bm, calendar)
    ear.to_parquet(path("processed") / "earnings_announcement_returns.parquet", index=False)
    pf = asof_merge(pf, ear, "available_date", ["ear", "ear_prev", "ear_sum2", "ear_pos_4", "ear_accel", "bm_date"])
    pf.loc[(pf.date - pf.bm_date).dt.days > 200, ["ear", "ear_prev", "ear_sum2", "ear_pos_4", "ear_accel"]] = np.nan

    # ---- accounting fundamentals (supplementary) -------------------------------------------------
    if fund is not None and len(fund):
        af = accounting_features(fund, bm)
        af.to_parquet(path("processed") / "accounting_features.parquet", index=False)
        acols = ["fy_end", "revenue", "ebitda", "pat", "fcf", "debt", "cash", "rev_g", "ebitda_g", "pat_g",
                 "ebitda_margin", "margin_chg", "rev_accel", "ebitda_vs_rev_g", "ocf_conv", "roce",
                 "debt_to_equity", "net_debt_ebitda", "turnaround"]
        pf = asof_merge(pf, af, "available_date", acols)
        mc = pf.mcap_cr * 1e7
        ev = mc + pf.debt.fillna(0) - pf.cash.fillna(0)
        pf["pe"] = (mc / pf.pat).where(pf.pat > 0)
        pf["ev_ebitda"] = (ev / pf.ebitda).where(pf.ebitda > 0)
        pf["ev_sales"] = ev / pf.revenue.where(pf.revenue > 0)
        pf["ps"] = mc / pf.revenue.where(pf.revenue > 0)
        pf["fcf_yield"] = pf.fcf / mc
        pf["earnings_yield"] = pf.pat / mc
        pf["peg"] = (pf.pe / (100 * pf.pat_g)).where((pf.pat_g > 0.05) & pf.pe.notna())

    # ---- sector ------------------------------------------------------------------------------------
    pf = pf.merge(sectors, on="symbol", how="left")
    pf["sector"] = pf.sector.fillna("Unclassified")

    # ---- universe ----------------------------------------------------------------------------------
    pf["exec_date"] = next_sessions(pd.DatetimeIndex(pf.date), calendar)
    excl = pf.symbol.str.contains(U["exclude_symbols_regex"], regex=True)
    series_ok = pf.merge(px[["symbol", "date", "series"]], on=["symbol", "date"], how="left").series.isin(["EQ", "BE"]).values
    pf["eligible_base"] = (pf.traded_today.fillna(False).astype(bool) & series_ok & ~excl &
                           (pf.hist_days >= 60) & (pf.adtv_63_cr >= U["min_median_traded_value_cr"]) &
                           pf.mcap_cr.notna())
    pf["mcap_rank"] = pf[pf.eligible_base].groupby("date").mcap_cr.rank(ascending=False, method="first")
    pf["eligible"] = pf.eligible_base & (pf.mcap_rank <= U["max_mcap_rank"])
    pf["segment"] = np.select([pf.mcap_rank <= U["large_cap_rank"], pf.mcap_rank <= U["mid_cap_rank"]],
                              ["large", "mid"], "small")
    pf.loc[pf.mcap_rank.isna(), "segment"] = None

    # ---- forward returns (close t+1 -> close of session after month-end t+h) ----------------------
    # forward-fill without limit after listing: a stock that stops trading is exited at its last close
    tr = px.pivot(index="date", columns="symbol", values="tr_index").sort_index().ffill()
    trs = tr.stack(future_stack=True)
    me_all = month_end_sessions(calendar, "2017-12-01", calendar[-1].strftime("%Y-%m-%d"))
    exec_all = next_sessions(me_all, calendar)
    exec_map = pd.Series(exec_all, index=me_all)
    b = n500.tri.reindex(calendar).ffill()
    for h in (1, 3, 6, 12):
        fwd_exec = exec_map.shift(-h)
        last_valid = calendar[-1]
        pf[f"exit_{h}m"] = pf.date.map(fwd_exec)
        e0 = list(zip(pf.exec_date, pf.symbol))
        e1 = list(zip(pf[f"exit_{h}m"], pf.symbol))
        p0 = pd.Series(trs.reindex(e0).values, index=pf.index)
        p1 = pd.Series(trs.reindex(e1).values, index=pf.index)
        pf[f"fwd_{h}m"] = p1 / p0 - 1
        pf[f"bench_{h}m"] = (pf[f"exit_{h}m"].map(b) / pf.exec_date.map(b) - 1).values
        pf[f"xs_{h}m"] = pf[f"fwd_{h}m"] - pf[f"bench_{h}m"]
        pf.loc[pf[f"exit_{h}m"].isna() | (pf[f"exit_{h}m"] > last_valid), [f"fwd_{h}m", f"xs_{h}m", f"bench_{h}m"]] = np.nan

    pf = pf.sort_values(["date", "symbol"]).reset_index(drop=True)
    out = ensure_dir(path("processed"))
    pf.to_parquet(out / "monthly_panel.parquet", index=False)
    register_dataset("monthly_panel", source_url="derived", coverage_start=str(pf.date.min().date()),
                     coverage_end=str(pf.date.max().date()), n_records=int(len(pf)),
                     n_eligible_rows=int(pf.eligible.sum()), quality_score=0.85,
                     limitations="Universe limited to symbols with SHP filings (survivorship gap for delisted names).",
                     path="data/processed/monthly_panel.parquet")
    log.info("panel: %d rows, eligible %d, dates %s..%s", len(pf), pf.eligible.sum(), pf.date.min(), pf.date.max())
    return pf


def load_sectors() -> pd.DataFrame:
    ref = path("raw") / "reference"
    parts = []
    for f in ("ind_niftytotalmarket_list.csv", "ind_nifty500list.csv", "ind_niftymicrocap250_list.csv"):
        fp = ref / f
        if fp.exists():
            d = pd.read_csv(fp)
            parts.append(d[["Symbol", "Industry"]].rename(columns={"Symbol": "symbol", "Industry": "sector"}))
    sec = pd.concat(parts).drop_duplicates("symbol")
    # historical names: sector-index membership history (any time) as fallback
    mem = pd.read_csv(path("raw") / "external" / "nse-historical-membership" / "index_history" / "data" /
                      "index_membership_history.csv")
    sector_idx = {"Nifty Bank": "Financial Services", "Nifty Financial Services": "Financial Services",
                  "Nifty IT": "Information Technology", "Nifty Pharma": "Healthcare", "Nifty Healthcare Index": "Healthcare",
                  "Nifty Auto": "Automobile and Auto Components", "Nifty FMCG": "Fast Moving Consumer Goods",
                  "Nifty Metal": "Metals & Mining", "Nifty Realty": "Realty", "Nifty Energy": "Oil Gas & Consumable Fuels",
                  "Nifty Media": "Media Entertainment & Publication", "Nifty PSU Bank": "Financial Services",
                  "Nifty Private Bank": "Financial Services", "Nifty Consumer Durables": "Consumer Durables",
                  "Nifty Oil & Gas": "Oil Gas & Consumable Fuels"}
    hm = mem[mem.index_name.isin(sector_idx)].copy()
    hm["sector"] = hm.index_name.map(sector_idx)
    hm = hm.drop_duplicates("symbol")[["symbol", "sector"]]
    hm = hm[~hm.symbol.isin(sec.symbol)]
    return pd.concat([sec, hm], ignore_index=True)
