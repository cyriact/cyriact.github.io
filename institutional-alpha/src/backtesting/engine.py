"""Monthly-frequency backtester on the PIT panel.

Mechanics
  * At each signal date t (month-end) a target portfolio is formed from eligible stocks.
  * Trades execute at the close of t+1; holding-period return = total return close(t+1) -> close(t'+1)
    where t' is the next rebalance date (dividends included via NSE ex-dates).
  * Drift: between rebalances weights drift with returns; turnover = sum |target - drifted|.
  * Costs (per side, by market-cap segment): brokerage, STT, exchange charges, SEBI fee, GST, stamp
    duty (buys) and slippage/impact.  Cash earns 0 (conservative).
  * Stocks that stop trading are exited at their last traded close (TR index forward-filled).
  * Buy-and-hold K-month variants use overlapping cohorts (Jegadeesh-Titman): each month 1/K of the
    book is re-formed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.common import SETTINGS
from src.portfolio.construction import select

C = SETTINGS["costs"]


def one_way_cost(segment: str | None, side: str) -> float:
    brok = C["brokerage_bps"]
    fees = C["exchange_txn_bps"] + C["sebi_fee_bps"]
    gst = C["gst_rate"] * (brok + fees)
    stt = C["stt_buy_bps"] if side == "buy" else C["stt_sell_bps"]
    stamp = C["stamp_duty_buy_bps"] if side == "buy" else 0.0
    slip = C["slippage_bps"].get(segment or "small", C["slippage_bps"]["small"])
    return (brok + fees + gst + stt + stamp + slip) / 1e4


@dataclass
class BTConfig:
    score_col: str
    top_n: int = 20
    weighting: str = "equal"
    max_weight: float = 0.05
    rebalance: str = "M"          # M monthly, Q quarterly (Jan/Apr/Jul/Oct month-ends), H6, H12 overlapping
    segment: str | None = None    # large / mid / small / None
    costs: bool = True
    start: str = SETTINGS["backtest"]["start"]
    end: str = SETTINGS["backtest"]["end"]
    exec_lag: int = 0             # extra months of delay (robustness)
    name: str = ""
    extra_filter: str | None = None  # pandas query applied to candidates


@dataclass
class BTResult:
    cfg: BTConfig
    returns: pd.DataFrame          # date, gross, net, cost, turnover, n_holdings, bench returns
    holdings: pd.DataFrame         # date, symbol, weight, score, fwd_1m, segment, sector
    extra: dict = field(default_factory=dict)


def _candidates(panel: pd.DataFrame, cfg: BTConfig) -> pd.DataFrame:
    p = panel[panel.eligible & panel[cfg.score_col].notna()]
    if cfg.segment:
        p = p[p.segment == cfg.segment]
    if cfg.extra_filter:
        p = p.query(cfg.extra_filter)
    return p


def _rebalance_dates(dates: pd.DatetimeIndex, rule: str) -> set:
    if rule == "Q":
        return {d for d in dates if d.month in (1, 4, 7, 10)}
    return set(dates)


def run(panel: pd.DataFrame, cfg: BTConfig, bench: pd.DataFrame) -> BTResult:
    """bench: DataFrame indexed by signal date with monthly returns columns (exec->next exec)."""
    # only signal dates whose 1-month holding period has an exit price inside the data
    dates = pd.DatetimeIndex(sorted(panel.loc[panel.exit_1m.notna(), "date"].unique()))
    dates = dates[(dates >= pd.Timestamp(cfg.start)) & (dates <= pd.Timestamp(cfg.end))]
    cands = _candidates(panel, cfg)
    by_date = {d: g for d, g in cands.groupby("date")}
    ret_lookup = panel.set_index(["date", "symbol"])[["fwd_1m", "segment", "sector"]]

    if cfg.rebalance in ("H6", "H12"):
        return _run_overlapping(panel, cfg, bench, dates, by_date, ret_lookup)

    rebal = _rebalance_dates(dates, cfg.rebalance)
    w_drift = pd.Series(dtype=float)
    rows, hold_rows = [], []
    target = pd.Series(dtype=float)
    for i, d in enumerate(dates):
        if d in rebal:
            sig_d = dates[i - cfg.exec_lag] if i - cfg.exec_lag >= 0 else None
            g = by_date.get(sig_d) if sig_d is not None else None
            target = select(g, cfg.score_col, cfg.top_n, cfg.weighting, cfg.max_weight) if g is not None else pd.Series(dtype=float)
        else:
            target = w_drift.copy()
        allsyms = target.index.union(w_drift.index)
        t_al = target.reindex(allsyms).fillna(0)
        d_al = w_drift.reindex(allsyms).fillna(0)
        trade = t_al - d_al
        seg = ret_lookup["segment"].reindex(list(zip([d] * len(allsyms), allsyms))).values
        seg = pd.Series(seg, index=allsyms).fillna("small")
        cost = 0.0
        if cfg.costs and d in rebal:
            buy_c = np.array([one_way_cost(s, "buy") for s in seg])
            sell_c = np.array([one_way_cost(s, "sell") for s in seg])
            cost = float((trade.clip(lower=0) * buy_c).sum() + ((-trade).clip(lower=0) * sell_c).sum())
        turnover = float(trade.abs().sum()) / 2 if d in rebal else 0.0
        r = ret_lookup["fwd_1m"].reindex(list(zip([d] * len(target), target.index))).values
        r = pd.Series(r, index=target.index)
        missing = r.isna()
        r = r.fillna(0.0)  # no forward price at all: treated as flat (rare; flagged)
        gross = float((target * r).sum())
        net = gross - cost
        rows.append({"date": d, "gross": gross, "net": net, "cost": cost, "turnover": turnover,
                     "n_holdings": int((target > 0).sum()), "invested": float(target.sum()),
                     "n_missing_ret": int(missing.sum())})
        for s, w in target.items():
            hold_rows.append({"date": d, "symbol": s, "weight": w, "fwd_1m": r[s]})
        # drift weights to next period
        grown = target * (1 + r)
        tot = 1 + gross  # cash stays flat
        w_drift = (grown / tot) if tot > 0 else grown
        w_drift = w_drift[w_drift > 1e-9]
    out = pd.DataFrame(rows).set_index("date")
    out = out.join(bench, how="left")
    return BTResult(cfg, out, pd.DataFrame(hold_rows))


def _run_overlapping(panel, cfg, bench, dates, by_date, ret_lookup) -> BTResult:
    K = 6 if cfg.rebalance == "H6" else 12
    cohorts: list[tuple[pd.Timestamp, pd.Series]] = []
    rows, hold_rows = [], []
    for i, d in enumerate(dates):
        g = by_date.get(d)
        w_new = select(g, cfg.score_col, cfg.top_n, cfg.weighting, cfg.max_weight) if g is not None else pd.Series(dtype=float)
        cohorts.append((d, w_new))
        cohorts = cohorts[-K:]
        cost = 0.0
        if cfg.costs:
            seg_new = pd.Series(ret_lookup["segment"].reindex(list(zip([d] * len(w_new), w_new.index))).values,
                                index=w_new.index).fillna("small")
            cost += float(sum(w * one_way_cost(seg_new[s], "buy") for s, w in w_new.items())) / K
            if len(cohorts) == K and i >= K:
                # the cohort formed K months ago is liquidated now (approximation: formation weights)
                old = cohorts[0][1]
                seg_old = pd.Series(ret_lookup["segment"].reindex(list(zip([d] * len(old), old.index))).values,
                                    index=old.index).fillna("small")
                cost += float(sum(w * one_way_cost(seg_old[s], "sell") for s, w in old.items())) / K
        rets = []
        agg = {}
        for _, w in cohorts:
            r = pd.Series(ret_lookup["fwd_1m"].reindex(list(zip([d] * len(w), w.index))).values, index=w.index).fillna(0.0)
            rets.append(float((w * r).sum()))
            for s, ww in w.items():
                agg[s] = agg.get(s, 0) + ww / len(cohorts)
        gross = float(np.mean(rets)) if rets else 0.0
        rows.append({"date": d, "gross": gross, "net": gross - cost, "cost": cost, "turnover": 1.0 / K,
                     "n_holdings": len(agg), "invested": float(sum(agg.values())), "n_missing_ret": 0})
        for s, ww in agg.items():
            hold_rows.append({"date": d, "symbol": s, "weight": ww})
    out = pd.DataFrame(rows).set_index("date").join(bench, how="left")
    return BTResult(cfg, out, pd.DataFrame(hold_rows))


def benchmark_returns(panel: pd.DataFrame, idx: pd.DataFrame) -> pd.DataFrame:
    """Monthly benchmark returns aligned with the panel's exec->next-exec holding periods."""
    pe = panel[["date", "exec_date", "exit_1m"]].drop_duplicates("date").set_index("date")
    out = pd.DataFrame(index=pe.index)
    for name, g in idx.groupby("index"):
        s = g.set_index("date").tri
        s = s.reindex(pd.DatetimeIndex(sorted(set(s.index) | set(pe.exec_date.dropna()) | set(pe.exit_1m.dropna())))).ffill()
        r = pe.exit_1m.map(s) / pe.exec_date.map(s) - 1
        out[f"b_{name}"] = r.values
    # equal-weighted eligible universe (size-neutral reference)
    el = panel[panel.eligible]
    out["b_Universe EW"] = el.groupby("date").fwd_1m.mean()
    for seg in ("large", "mid", "small"):
        out[f"b_Universe EW {seg}"] = el[el.segment == seg].groupby("date").fwd_1m.mean()
    return out
