"""Run every backtest and statistical test; write tables and charts to outputs/."""
from __future__ import annotations

import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.backtesting import walk_forward
from src.backtesting.engine import BTConfig, benchmark_returns, run
from src.common import SETTINGS, ensure_dir, get_logger, path
from src.performance import metrics as M
from src.performance import predictive as P
from src.screening.scoring import strategy_scores

log = get_logger("research")
OUT = path("outputs")
TAB = ensure_dir(OUT / "tables")
CH = ensure_dir(OUT / "charts")
BT = SETTINGS["backtest"]

PRIMARY = {
    "A_block": "score_A_block",
    "B_shareholding": "score_B_shareholding",
    "C_inst_earnings": "score_C_inst_earnings",
    "D_inst_block_earnings": "score_D_inst_block_earnings",
    "E_full": "score_E_full",
}
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]


# ----------------------------------------------------------------------------- scoring
def add_scores(panel: pd.DataFrame) -> pd.DataFrame:
    sc = strategy_scores(panel)
    p = pd.concat([panel, sc], axis=1)
    # single-factor scores (for the research questions)
    el = p.eligible
    def r(col, d=1):
        return (p[col] * d).where(el).groupby(p.date).rank(pct=True) * 100
    p["score_FII_only"] = r("d_fii_1q")
    p["score_MF_only"] = r("d_mf_1q")
    p["score_FII_2q"] = r("d_fii_2q")
    p["score_MF_2q"] = r("d_mf_2q")
    p["score_INST_1q"] = r("d_inst_1q")
    both = (p.d_fii_1q > 0) & (p.d_mf_1q > 0)
    p["score_FII_MF_both"] = np.where(both, 50 + (r("d_fii_1q") + r("d_mf_1q")) / 4, np.where(p.d_inst_1q.notna(), r("d_inst_1q") / 2, np.nan))
    p.loc[~el, "score_FII_MF_both"] = np.nan
    p["score_INSURANCE_only"] = r("d_insurance_1q")
    p["score_EAR_only"] = p["score_earnings"].where(p.ear.notna())
    p["score_MOM_only"] = r("rs_6m")
    p["score_absorption"] = np.where(p.absorption_event == True, 100, np.where(p.nh_large_exit_pp.notna(), 0, np.nan))  # noqa: E712
    p.loc[~el, "score_absorption"] = np.nan
    p["score_named_credible"] = r("nh_credible_increase_pp")
    p["score_delivery_absorb"] = r("absorb_value_63")
    p["score_short_horizon_buy"] = r("nh_short_horizon_buy_pp")
    p["score_passive_buy"] = r("nh_passive_buy_pp")
    p["score_foreign_lo_named_buy"] = r("nh_foreign_lo_buy_pp")
    p["score_domestic_mf_named_buy"] = r("nh_domestic_mf_buy_pp")
    p["score_buyer_quality"] = r("nh_buyer_quality")
    # accounting-earnings variant (only where Yahoo fundamentals are available, mid-2024+)
    acct = ["rev_g", "rev_accel", "ebitda_vs_rev_g", "pat_g", "margin_chg", "ocf_conv", "turnaround"]
    ar = pd.concat([r(c) for c in acct], axis=1).mean(axis=1)
    has_acct = p[acct].notna().sum(axis=1) >= 3
    p["score_acct_earnings"] = ar.where(has_acct)
    p["score_C_acct"] = (0.5 * p.score_B_shareholding + 0.5 * p.score_acct_earnings).where(has_acct)
    p["score_D_no_val"] = p["score_W_no_valuation"]
    # single-component portfolios of the 100-point model (which component carries the composite?)
    base_ok = p["score_E_full"].notna()
    for k in ("earnings", "institutional", "quality", "valuation", "price_volume"):
        p[f"score_comp_{k}"] = (p[f"c_{k}"] * 100).where(base_ok)
    return p


# ----------------------------------------------------------------------------- backtests
def bt_table(panel, bench, configs: list[BTConfig], bench_cols=("b_Nifty 500", "b_Universe EW")) -> tuple[pd.DataFrame, dict]:
    rows, results = [], {}
    for cfg in configs:
        res = run(panel, cfg, bench)
        results[cfg.name] = res
        r = res.returns
        for bcol in bench_cols:
            bc = bcol
            if cfg.segment and bcol == "b_Universe EW":
                bc = f"b_Universe EW {cfg.segment}"
            st = M.perf_stats(r.net, r[bc] if bc in r else None, cfg.name, n_boot=1000 if bcol == "b_Nifty 500" else 0)
            st.update({"benchmark": bc.replace("b_", ""), "gross_CAGR": M.cagr(r.gross), "avg_cost_ann": r.cost.mean() * 12,
                       "avg_turnover_m": r.turnover[r.turnover > 0].mean(), "avg_holdings": r.n_holdings.mean(),
                       "top_n": cfg.top_n, "weighting": cfg.weighting, "rebalance": cfg.rebalance,
                       "segment": cfg.segment or "all", "costs": cfg.costs, "score": cfg.score_col})
            rows.append(st)
    return pd.DataFrame(rows), results


def make_configs() -> list[BTConfig]:
    cf = []
    for k, s in PRIMARY.items():
        cf.append(BTConfig(s, name=k))
    for k, s in PRIMARY.items():
        for seg in ("large", "mid", "small"):
            cf.append(BTConfig(s, segment=seg, name=f"{k}|{seg}"))
    for k in ("B_shareholding", "D_inst_block_earnings", "E_full"):
        s = PRIMARY[k]
        cf += [BTConfig(s, top_n=10, max_weight=0.10, name=f"{k}|top10"),
               BTConfig(s, top_n=30, name=f"{k}|top30"),
               BTConfig(s, top_n=30, weighting="score", name=f"{k}|top30_scorew"),
               BTConfig(s, rebalance="Q", name=f"{k}|quarterly"),
               BTConfig(s, rebalance="H6", name=f"{k}|hold6m"),
               BTConfig(s, rebalance="H12", name=f"{k}|hold12m"),
               BTConfig(s, costs=False, name=f"{k}|gross_no_costs"),
               BTConfig(s, exec_lag=1, name=f"{k}|entry_lag_1m")]
    for w in ("base", "equal", "inst_heavy", "earn_heavy", "no_valuation", "simple_inst_momentum"):
        cf.append(BTConfig(f"score_W_{w}", name=f"W_{w}"))
    for f in ("FII_only", "FII_2q", "MF_only", "MF_2q", "INST_1q", "FII_MF_both", "INSURANCE_only", "EAR_only",
              "MOM_only", "named_credible", "delivery_absorb", "short_horizon_buy", "foreign_lo_named_buy",
              "domestic_mf_named_buy"):
        cf.append(BTConfig(f"score_{f}", name=f"F_{f}"))
    for k in ("earnings", "institutional", "quality", "valuation", "price_volume"):
        cf.append(BTConfig(f"score_comp_{k}", name=f"COMP_{k}"))
    # valuation used as a filter (exclude the most expensive 40% on the valuation component)
    cf.append(BTConfig(PRIMARY["D_inst_block_earnings"], extra_filter="c_valuation >= 0.4", name="D_inst_block_earnings|valuation_filter"))
    cf.append(BTConfig(PRIMARY["B_shareholding"], extra_filter="c_valuation >= 0.4", name="B_shareholding|valuation_filter"))
    cf.append(BTConfig(PRIMARY["C_inst_earnings"], extra_filter="c_valuation >= 0.4", name="C_inst_earnings|valuation_filter"))
    # walk-forward weights exist only from the first re-estimation (Jan-2021): compare like with like
    cf.append(BTConfig("score_WF", start="2021-01-01", name="E_walk_forward"))
    cf.append(BTConfig("score_E_full", start="2021-01-01", name="E_full(2021+)"))
    cf.append(BTConfig("score_W_simple_inst_momentum", start="2021-01-01", name="W_simple_inst_momentum(2021+)"))
    return cf


# ----------------------------------------------------------------------------- charts
def _style(ax, title):
    ax.set_title(title, fontsize=11, loc="left", fontweight="bold")
    ax.grid(alpha=0.25)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def chart_cumulative(results, names, bench, fname, title):
    fig, ax = plt.subplots(figsize=(10, 5.2))
    for i, n in enumerate(names):
        r = results[n].returns.net
        ax.plot((1 + r).cumprod(), label=n, color=PALETTE[i % len(PALETTE)], lw=1.6)
    for bcol, c, ls in (("b_Nifty 500", "black", "--"), ("b_Universe EW", "#888", ":")):
        b = bench[bcol].reindex(results[names[0]].returns.index)
        ax.plot((1 + b.fillna(0)).cumprod(), label=bcol.replace("b_", "") + (" TRI (approx.)" if "Nifty" in bcol else ""),
                color=c, ls=ls, lw=1.3)
    ax.set_yscale("log")
    from matplotlib.ticker import FuncFormatter, NullFormatter
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.set_yticks([0.6, 0.8, 1, 1.5, 2, 3, 4, 5])
    ax.legend(fontsize=8, frameon=False, ncol=2)
    _style(ax, title)
    ax.set_ylabel("Growth of 1 (log, net of costs)")
    fig.tight_layout()
    fig.savefig(CH / fname, dpi=130)
    plt.close(fig)


def chart_drawdown(results, names, bench, fname):
    fig, ax = plt.subplots(figsize=(10, 4))
    for i, n in enumerate(names):
        ax.plot(M.drawdown_series(results[n].returns.net), label=n, color=PALETTE[i % len(PALETTE)], lw=1.3)
    b = bench["b_Nifty 500"].reindex(results[names[0]].returns.index)
    ax.fill_between(b.index, M.drawdown_series(b), 0, color="black", alpha=0.12, label="Nifty 500 TRI")
    ax.legend(fontsize=8, frameon=False, ncol=3)
    _style(ax, "Drawdowns (monthly, net)")
    fig.tight_layout()
    fig.savefig(CH / fname, dpi=130)
    plt.close(fig)


def chart_rolling(results, names, bench, fname):
    fig, ax = plt.subplots(figsize=(10, 4))
    for i, n in enumerate(names):
        r = results[n].returns
        ax.plot(M.rolling_12m(r.net) - M.rolling_12m(r["b_Nifty 500"]), label=n, color=PALETTE[i % len(PALETTE)], lw=1.3)
    ax.axhline(0, color="black", lw=0.8)
    ax.legend(fontsize=8, frameon=False, ncol=3)
    _style(ax, "Rolling 12-month excess return vs Nifty 500 TRI (net)")
    fig.tight_layout()
    fig.savefig(CH / fname, dpi=130)
    plt.close(fig)


def chart_deciles(dec: pd.DataFrame, fname: str, title: str):
    d = dec[dec.bucket != "Top-Bottom"]
    sigs = d.score.unique()
    fig, axes = plt.subplots(1, len(sigs), figsize=(3.2 * len(sigs), 3.4), sharey=True)
    axes = np.atleast_1d(axes)
    for ax, s in zip(axes, sigs):
        x = d[d.score == s]
        ax.bar(x.bucket.astype(int), x["mean"] * 100, color=["#b23a48" if v < 0 else "#1f5fa8" for v in x["mean"]])
        ax.set_title(s.replace("score_", ""), fontsize=9)
        ax.axhline(0, color="black", lw=0.6)
        ax.set_xlabel("decile (10 = highest score)", fontsize=8)
    axes[0].set_ylabel("mean excess return vs Nifty 500, %")
    fig.suptitle(title, fontsize=11, x=0.01, ha="left", fontweight="bold")
    fig.tight_layout()
    fig.savefig(CH / fname, dpi=130)
    plt.close(fig)


# ----------------------------------------------------------------------------- analyses
def regime_table(results, names, bench) -> pd.DataFrame:
    b = bench["b_Nifty 500"]
    n500_6m = (1 + b).rolling(6).apply(np.prod, raw=True).shift(1) - 1  # trailing at signal date
    rows = []
    for n in names:
        r = results[n].returns
        ex = r.net - r["b_Nifty 500"]
        for lab, mask in (("bull (trailing 6m N500 >= 0)", n500_6m.reindex(r.index) >= 0),
                          ("bear (trailing 6m N500 < 0)", n500_6m.reindex(r.index) < 0),
                          ("up month (N500 >= 0)", r["b_Nifty 500"] >= 0),
                          ("down month (N500 < 0)", r["b_Nifty 500"] < 0)):
            e = ex[mask]
            m, t, p = M.nw_tstat(e, 3)
            rows.append({"strategy": n, "regime": lab, "months": int(mask.sum()), "mean_excess_m": m, "t_nw": t,
                         "hit_rate": (e > 0).mean(), "strategy_mean_m": r.net[mask].mean(), "bench_mean_m": r["b_Nifty 500"][mask].mean()})
    return pd.DataFrame(rows)


def subperiod_table(results, names) -> pd.DataFrame:
    periods = {"development 2018-07..2021-12": (BT["start"], BT["dev_end"]),
               "validation 2022-01..2023-12": ("2022-01-01", BT["val_end"]),
               "out-of-sample 2024-01..2026-10": ("2024-01-01", BT["end"]),
               "full": (BT["start"], BT["end"])}
    rows = []
    for n in names:
        r = results[n].returns
        for lab, (a, b) in periods.items():
            rr = r.loc[a:b]
            st = M.perf_stats(rr.net, rr["b_Nifty 500"], n, n_boot=0)
            st2 = M.perf_stats(rr.net, rr["b_Universe EW"], n, n_boot=0)
            rows.append({"strategy": n, "period": lab, "months": st.get("months"), "CAGR": st.get("CAGR"),
                         "N500_CAGR": st.get("bench_CAGR"), "excess_vs_N500": st.get("excess_CAGR"),
                         "t_excess_vs_N500": st.get("t_excess_nw"), "UEW_CAGR": st2.get("bench_CAGR"),
                         "excess_vs_UEW": st2.get("excess_CAGR"), "t_excess_vs_UEW": st2.get("t_excess_nw"),
                         "sharpe": st.get("sharpe"), "max_dd": st.get("max_dd")})
    return pd.DataFrame(rows)


def concentration_table(results, names, panel) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, contrib_rows = [], []
    for n in names:
        h = results[n].holdings.copy()
        h = h.merge(panel[["date", "symbol", "fwd_1m", "sector"]].rename(columns={"fwd_1m": "r"}), on=["date", "symbol"], how="left")
        h["contrib"] = h.weight * h.r.fillna(0)
        by = h.groupby("symbol").contrib.sum().sort_values(ascending=False)
        tot = by.sum()
        r = results[n].returns
        base = M.cagr(r.net)
        # remove the top-k contributors' contributions month by month
        out = {"strategy": n, "sum_monthly_contrib": tot, "top5_share_of_sum": by.head(5).sum() / tot if tot else np.nan,
               "top10_share_of_sum": by.head(10).sum() / tot if tot else np.nan, "n_names_held": by.size,
               "CAGR_net": base}
        for k in (1, 5, 10):
            drop = set(by.head(k).index)
            adj = r.net - h[h.symbol.isin(drop)].groupby("date").contrib.sum().reindex(r.index).fillna(0)
            out[f"CAGR_ex_top{k}"] = M.cagr(adj)
            out[f"excess_ex_top{k}_vs_N500"] = M.cagr(adj) - M.cagr(r["b_Nifty 500"])
        # winsorised stock returns (cap each stock-month at the 99th pct of all holdings)
        cap = h.r.quantile(0.99)
        adjw = r.net - (h.weight * (h.r - h.r.clip(upper=cap)).fillna(0)).groupby(h.date).sum().reindex(r.index).fillna(0)
        out["CAGR_winsor99"] = M.cagr(adjw)
        rows.append(out)
        top = by.head(15).rename("sum_contrib").reset_index()
        top["strategy"] = n
        contrib_rows.append(top)
    return pd.DataFrame(rows), pd.concat(contrib_rows)


def sector_table(results, names, panel) -> pd.DataFrame:
    rows = []
    for n in names:
        h = results[n].holdings.merge(panel[["date", "symbol", "sector", "xs_1m", "segment"]], on=["date", "symbol"], how="left")
        g = h.groupby("sector").agg(avg_weight=("weight", lambda s: s.sum() / h.date.nunique()),
                                    stock_months=("symbol", "size"), mean_xs_1m=("xs_1m", "mean"),
                                    hit_rate=("xs_1m", lambda s: (s > 0).mean()))
        g["strategy"] = n
        rows.append(g.reset_index())
    return pd.concat(rows)


def false_positives(panel, score_col, h=6, top_n=20, k=40) -> pd.DataFrame:
    p = panel[panel.eligible & panel[score_col].notna() & panel[f"xs_{h}m"].notna()]
    top = p.sort_values(["date", score_col], ascending=[True, False]).groupby("date").head(top_n)
    cols = ["date", "symbol", "segment", "sector", score_col, f"fwd_{h}m", f"xs_{h}m", "d_fii_1q", "d_mf_1q", "d_inst_1q",
            "nh_credible_increase_pp", "absorption_event", "ear", "rs_6m", "mcap_cr"]
    worst = top.sort_values(f"xs_{h}m").head(k)[[c for c in cols if c in top.columns]]
    best = top.sort_values(f"xs_{h}m", ascending=False).head(k)[[c for c in cols if c in top.columns]]
    worst["kind"], best["kind"] = "false_positive", "winner"
    return pd.concat([worst, best])


# ----------------------------------------------------------------------------- main
def run_research(panel: pd.DataFrame, idx: pd.DataFrame) -> dict:
    p = add_scores(panel)
    # walk-forward weights
    cand = p[p.eligible & p.score_E_full.notna()].copy()
    chosen, wf_score, insample = walk_forward.run(cand)
    p["score_WF"] = wf_score.reindex(p.index)
    chosen.to_csv(TAB / "walk_forward_weights.csv", index=False)
    insample.to_csv(TAB / "insample_optimal_weights_LOOKAHEAD_reference.csv", index=False)
    p.to_parquet(path("processed") / "panel_scored.parquet", index=False)

    bench = benchmark_returns(p, idx)
    bench.to_csv(TAB / "benchmark_monthly_returns.csv")
    configs = make_configs()
    perf, results = bt_table(p, bench, configs)
    perf.to_csv(TAB / "strategy_performance.csv", index=False)
    # accounting-earnings window comparison (Yahoo fundamentals: signals from mid-2024)
    acct_start = "2024-07-01"
    acc_cfgs = [BTConfig("score_C_acct", start=acct_start, name="C_acct_earnings(2024-07+)"),
                BTConfig("score_B_shareholding", start=acct_start, name="B_shareholding(2024-07+)"),
                BTConfig("score_C_inst_earnings", start=acct_start, name="C_EARproxy(2024-07+)"),
                BTConfig("score_E_full", start=acct_start, name="E_full(2024-07+)"),
                BTConfig("score_D_no_val", start=acct_start, name="E_no_valuation(2024-07+)")]
    perf_acct, res_acct = bt_table(p, bench, acc_cfgs)
    perf_acct.to_csv(TAB / "accounting_window_performance.csv", index=False)
    results.update(res_acct)

    names = list(PRIMARY)
    monthly = pd.DataFrame({n: results[n].returns.net for n in names + ["E_walk_forward"]})
    for b in ("b_Nifty 500", "b_Nifty 50", "b_Nifty Midcap 150", "b_Nifty Smallcap 250", "b_Universe EW"):
        if b in bench:
            monthly[b.replace("b_", "")] = bench[b].reindex(monthly.index)
    monthly.to_csv(TAB / "monthly_returns_net.csv")
    M.annual_returns(monthly).to_csv(TAB / "annual_returns.csv")
    roll = monthly.apply(M.rolling_12m)
    roll.to_csv(TAB / "rolling_12m_returns.csv")

    subperiod_table(results, names + ["E_walk_forward", "E_full(2021+)", "W_simple_inst_momentum", "F_MOM_only"]).to_csv(TAB / "subperiod_performance.csv", index=False)
    regime_table(results, names, bench).to_csv(TAB / "regime_performance.csv", index=False)
    conc, contrib = concentration_table(results, names, p)
    conc.to_csv(TAB / "concentration_extreme_winners.csv", index=False)
    contrib.to_csv(TAB / "top_contributors.csv", index=False)
    sector_table(results, names, p).to_csv(TAB / "sector_attribution.csv", index=False)
    fp = pd.concat([false_positives(p, PRIMARY[n]).assign(strategy=n) for n in ("D_inst_block_earnings", "E_full")])
    fp.to_csv(TAB / "false_positives_and_winners.csv", index=False)

    # portfolio characteristics
    chars = []
    for n in names:
        h = results[n].holdings.merge(p[["date", "symbol", "segment", "mcap_cr", "adtv_63_cr", "sector"]], on=["date", "symbol"], how="left")
        r = results[n].returns
        # average holding period (months) from consecutive presence
        pres = h.pivot_table(index="date", columns="symbol", values="weight", aggfunc="sum").notna()
        spells = []
        for s in pres.columns:
            x = pres[s].astype(int).values
            run_len = 0
            for v in x:
                if v:
                    run_len += 1
                elif run_len:
                    spells.append(run_len)
                    run_len = 0
            if run_len:
                spells.append(run_len)
        seg_mix = h.groupby("segment").weight.sum() / h.weight.sum()
        sec = h.groupby("sector").weight.sum() / h.weight.sum()
        chars.append({"strategy": n, "avg_holdings": r.n_holdings.mean(), "avg_monthly_turnover": r.turnover.mean(),
                      "annual_turnover": r.turnover.mean() * 12, "avg_cost_bp_per_year": r.cost.mean() * 12 * 1e4,
                      "median_mcap_cr": h.mcap_cr.median(), "median_adtv_cr": h.adtv_63_cr.median(),
                      "p10_adtv_cr": h.adtv_63_cr.quantile(0.1),
                      "w_large": seg_mix.get("large", 0), "w_mid": seg_mix.get("mid", 0), "w_small": seg_mix.get("small", 0),
                      "top_sector": sec.idxmax(), "top_sector_weight": sec.max(), "hhi_sector": float((sec ** 2).sum()),
                      "avg_holding_period_m": float(np.mean(spells)) if spells else np.nan,
                      "avg_invested": r.invested.mean()})
    pd.DataFrame(chars).to_csv(TAB / "portfolio_characteristics.csv", index=False)

    # predictive tests
    sig = [PRIMARY[n] for n in names] + ["score_FII_only", "score_FII_2q", "score_MF_only", "score_MF_2q", "score_INST_1q",
                                          "score_FII_MF_both", "score_INSURANCE_only", "score_named_credible",
                                          "score_delivery_absorb", "score_short_horizon_buy", "score_passive_buy",
                                          "score_foreign_lo_named_buy", "score_domestic_mf_named_buy", "score_buyer_quality",
                                          "score_earnings", "score_EAR_only", "score_MOM_only",
                                          "score_acct_earnings", "score_W_base", "score_W_simple_inst_momentum", "score_WF",
                                          "score_comp_earnings", "score_comp_institutional", "score_comp_quality",
                                          "score_comp_valuation", "score_comp_price_volume"]
    P.ic_summary(p, sig).to_csv(TAB / "ic_summary.csv", index=False)
    dec = pd.concat([P.decile_table(p, s, f"xs_{h}m", h) .assign(horizon=h) for s in sig for h in (1, 3, 6, 12)])
    dec.to_csv(TAB / "decile_returns.csv", index=False)
    hits = pd.DataFrame([P.top_vs_universe_hit_rates(p, s, h) for s in sig for h in (1, 3, 6, 12)])
    hits.to_csv(TAB / "hit_rates_top20.csv", index=False)
    feats = ["d_fii_1q", "d_mf_1q", "d_insurance_1q", "d_fii_2q", "d_mf_2q", "inst_acc_streak", "nh_credible_increase_pp",
             "absorb_value_63", "ear", "ear_accel", "rs_6m", "mom_12_1", "vol_1y", "div_yield_ttm", "rev_36m", "deliv_chg",
             "updown_vol_63"]
    fm = pd.concat([P.fama_macbeth(p, feats, f"xs_{h}m", h).assign(horizon=h) for h in (1, 3, 6, 12)])
    fm.to_csv(TAB / "fama_macbeth.csv", index=False)
    # absorption event study (quarterly SHP proxy)
    ev = absorption_event_study(p)
    ev.to_csv(TAB / "absorption_event_study.csv", index=False)
    filing_event_study(p)
    current_screen(p)

    # charts
    chart_cumulative(results, names, bench, "cumulative_primary.png", "Strategies A-E vs Nifty 500 TRI (net of costs)")
    chart_drawdown(results, ["B_shareholding", "D_inst_block_earnings", "E_full"], bench, "drawdowns.png")
    chart_rolling(results, ["B_shareholding", "D_inst_block_earnings", "E_full"], bench, "rolling_12m_excess.png")
    for seg in ("large", "mid", "small"):
        chart_cumulative(results, [f"{n}|{seg}" for n in names], bench, f"cumulative_{seg}.png", f"Strategies A-E — {seg} caps (net)")
    chart_cumulative(results, ["F_FII_only", "F_MF_only", "F_FII_MF_both", "F_INST_1q", "F_named_credible", "F_EAR_only", "F_MOM_only"],
                     bench, "cumulative_single_factors.png", "Single-signal top-20 portfolios (net)")
    d3 = dec[(dec.horizon == 3) & dec.score.isin([PRIMARY[n] for n in names])]
    chart_deciles(d3, "deciles_3m_primary.png", "Mean 3-month excess return by score decile (Strategies A-E)")
    d3f = dec[(dec.horizon == 3) & dec.score.isin(["score_FII_only", "score_MF_only", "score_INST_1q", "score_named_credible", "score_EAR_only"])]
    chart_deciles(d3f, "deciles_3m_single.png", "Mean 3-month excess return by decile (single signals)")
    return {"panel": p, "bench": bench, "results": results, "perf": perf, "perf_acct": perf_acct}


def absorption_event_study(p: pd.DataFrame) -> pd.DataFrame:
    """Compare forward excess returns after (i) large-holder exits absorbed by institutions,
    (ii) large exits NOT absorbed, (iii) no large exit — first signal month after each filing."""
    q = p[p.eligible & p.nh_large_exit_pp.notna()].copy()
    q = q.sort_values("date").drop_duplicates(["symbol", "sig_period_end"], keep="first")
    q["group"] = np.select([q.absorption_event == True, q.nh_large_exit_pp >= 2.0],  # noqa: E712
                           ["exit absorbed by institutions", "large exit not absorbed"], "no large exit")
    rows = []
    for gname, g in q.groupby("group"):
        for h in (1, 3, 6, 12):
            x = g[f"xs_{h}m"].dropna()
            ts = g.groupby("date")[f"xs_{h}m"].mean()
            m, t, pv = M.nw_tstat(ts, max(3, h))
            rows.append({"group": gname, "horizon_m": h, "n_events": len(x), "mean_xs": x.mean(), "median_xs": x.median(),
                         "hit_rate": (x > 0).mean(), "date_mean_xs": m, "t_nw_dates": t})
    return pd.DataFrame(rows)


def filing_event_study(panel: pd.DataFrame) -> pd.DataFrame:
    """Event-time test at the true disclosure date: entry at the close of the first session after each
    quarterly SHP broadcast; forward 1/3/6/12-month total returns minus Nifty 500 TRI; stocks bucketed
    into quintiles of the disclosed change (within each calendar quarter of disclosure). Only filings of
    stocks eligible at the preceding month-end are used."""
    proc = path("processed")
    q = pd.read_parquet(proc / "shareholding_quarterly.parquet")
    px = pd.read_parquet(proc / "prices_daily.parquet", columns=["symbol", "date", "tr_index"])
    idx = pd.read_parquet(proc / "index_levels.parquet")
    n5 = idx[idx["index"] == "Nifty 500"].set_index("date").tri
    tr = px.pivot(index="date", columns="symbol", values="tr_index").sort_index().ffill()
    cal = tr.index
    el = panel.loc[panel.eligible, ["date", "symbol", "segment"]]
    q = q.dropna(subset=["effective_date", "d_inst_1q"]).copy()
    me = pd.DatetimeIndex(sorted(el.date.unique()))
    q["prior_me"] = me[np.clip(me.searchsorted(q.effective_date, side="left") - 1, 0, len(me) - 1)]
    q = q.merge(el.rename(columns={"date": "prior_me"}), on=["prior_me", "symbol"], how="inner")
    rows = []
    for h in (1, 3, 6, 12):
        ex = q.effective_date + pd.DateOffset(months=h)
        ok = ex <= cal[-1]
        e_i = cal.searchsorted(q.effective_date[ok])
        x_i = cal.searchsorted(ex[ok], side="right") - 1
        cols = tr.columns.get_indexer(q.symbol[ok])
        valid = cols >= 0
        r = np.full(ok.sum(), np.nan)
        trv = tr.to_numpy()
        r[valid] = trv[x_i[valid], cols[valid]] / trv[e_i[valid], cols[valid]] - 1
        b = n5.reindex(cal).ffill().to_numpy()
        rb = b[x_i] / b[e_i] - 1
        q.loc[ok, f"ev_xs_{h}m"] = r - rb
    q["disc_q"] = q.effective_date.dt.to_period("Q")
    for sig in ("d_fii_1q", "d_mf_1q", "d_inst_1q", "d_insurance_1q", "flow_inst_1q"):
        qq = q.dropna(subset=[sig]).copy()
        qq["quint"] = qq.groupby("disc_q")[sig].transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) + 1)
        for h in (1, 3, 6, 12):
            col = f"ev_xs_{h}m"
            ts = qq.groupby(["disc_q", "quint"])[col].mean().unstack()
            for k in ts.columns:
                m, t, p = M.nw_tstat(ts[k], max(1, h // 3))
                rows.append({"signal": sig, "horizon_m": h, "quintile": int(k), "mean_xs": m, "t_nw_quarters": t,
                             "median_xs": qq.loc[qq.quint == k, col].median(), "n_events": int(qq.loc[qq.quint == k, col].notna().sum())})
            spread = ts[ts.columns.max()] - ts[ts.columns.min()]
            m, t, p = M.nw_tstat(spread, max(1, h // 3))
            rows.append({"signal": sig, "horizon_m": h, "quintile": "Q5-Q1", "mean_xs": m, "t_nw_quarters": t,
                         "median_xs": np.nan, "n_events": int(spread.notna().sum())})
    out = pd.DataFrame(rows)
    out.to_csv(TAB / "filing_event_study.csv", index=False)
    return out


def current_screen(p: pd.DataFrame, top: int = 30) -> pd.DataFrame:
    """Latest signal date (live): top names by D and E with the evidence behind each score."""
    d = p.date.max()
    g = p[(p.date == d) & p.eligible].copy()
    cols = ["symbol", "segment", "sector", "mcap_cr", "adtv_63_cr", "score_D_inst_block_earnings", "score_E_full",
            "c_earnings", "c_institutional", "c_quality", "c_valuation", "c_price_volume", "sig_period_end",
            "d_fii_1q", "d_mf_1q", "d_inst_1q", "inst_acc_streak", "nh_credible_increase_pp", "nh_n_buyer_families",
            "absorption_event", "ear", "rev_g", "pat_g", "pe", "ev_ebitda", "rs_6m"]
    out = []
    for s in ("score_D_inst_block_earnings", "score_E_full"):
        x = g.dropna(subset=[s]).sort_values(s, ascending=False).head(top)[[c for c in cols if c in g.columns]].copy()
        x.insert(0, "rank", range(1, len(x) + 1))
        x.insert(0, "list", s.replace("score_", ""))
        out.append(x)
    res = pd.concat(out)
    res.insert(0, "signal_date", d.date())
    res.to_csv(TAB / "current_screen_latest.csv", index=False)
    return res
