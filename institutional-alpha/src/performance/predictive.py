"""Cross-sectional predictive tests: deciles, information coefficients, Fama-MacBeth regressions.

Overlapping horizons: for h-month forward returns sampled monthly, consecutive observations
overlap by h-1 months, so time-series t-stats use Newey-West with h (>=3) lags rather than
treating observations as independent.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import statsmodels.api as sm

from src.performance.metrics import nw_tstat


def _lags(h: int) -> int:
    return max(3, h)


def decile_table(panel: pd.DataFrame, score: str, ret: str, h: int, q: int = 10, min_n: int = 50) -> pd.DataFrame:
    p = panel[panel.eligible & panel[score].notna() & panel[ret].notna()][["date", score, ret]].copy()
    cnt = p.groupby("date")[score].transform("count")
    p = p[cnt >= min_n]
    p["bucket"] = p.groupby("date")[score].transform(lambda s: pd.qcut(s.rank(method="first"), q, labels=False) + 1)
    if p.empty:
        return pd.DataFrame(columns=["bucket", "mean", "t_nw", "median_of_dates", "n_dates", "score", "ret"])
    ts = p.groupby(["date", "bucket"])[ret].mean().unstack()
    rows = []
    for b in ts.columns:
        m, t, pv = nw_tstat(ts[b], _lags(h))
        rows.append({"bucket": int(b), "mean": m, "t_nw": t, "median_of_dates": ts[b].median(), "n_dates": ts[b].notna().sum()})
    spread = ts[ts.columns.max()] - ts[ts.columns.min()]
    m, t, pv = nw_tstat(spread, _lags(h))
    rows.append({"bucket": "Top-Bottom", "mean": m, "t_nw": t, "median_of_dates": spread.median(), "n_dates": spread.notna().sum()})
    out = pd.DataFrame(rows)
    out["score"], out["ret"] = score, ret
    return out


def decile_timeseries(panel: pd.DataFrame, score: str, ret: str, q: int = 10, min_n: int = 50) -> pd.DataFrame:
    p = panel[panel.eligible & panel[score].notna() & panel[ret].notna()][["date", score, ret]].copy()
    cnt = p.groupby("date")[score].transform("count")
    p = p[cnt >= min_n]
    p["bucket"] = p.groupby("date")[score].transform(lambda s: pd.qcut(s.rank(method="first"), q, labels=False) + 1)
    return p.groupby(["date", "bucket"])[ret].mean().unstack()


def ic_series(panel: pd.DataFrame, score: str, ret: str, min_n: int = 50) -> pd.Series:
    p = panel[panel.eligible & panel[score].notna() & panel[ret].notna()]
    def f(g):
        if len(g) < min_n:
            return np.nan
        return g[score].rank().corr(g[ret].rank())
    if p.empty:
        return pd.Series(dtype=float)
    res = p.groupby("date").apply(f, include_groups=False)
    return res if isinstance(res, pd.Series) else pd.Series(dtype=float)


def ic_summary(panel: pd.DataFrame, scores: list[str], horizons=(1, 3, 6, 12)) -> pd.DataFrame:
    rows = []
    for s in scores:
        for h in horizons:
            ic = ic_series(panel, s, f"xs_{h}m")
            m, t, p = nw_tstat(ic, _lags(h))
            rows.append({"signal": s, "horizon_m": h, "mean_IC": m, "t_nw": t, "p": p,
                         "IC_pos_share": (ic > 0).mean(), "n_dates": ic.notna().sum()})
    return pd.DataFrame(rows)


def fama_macbeth(panel: pd.DataFrame, features: list[str], ret: str, h: int, min_n: int = 100) -> pd.DataFrame:
    """Monthly cross-sectional OLS of returns on rank-normalised features (z of percentile ranks);
    NaN features set to 0 (cross-sectional median) with the share of imputation reported."""
    p = panel[panel.eligible & panel[ret].notna()].copy()
    coefs = []
    for d, g in p.groupby("date"):
        if len(g) < min_n:
            continue
        X = pd.DataFrame(index=g.index)
        for f in features:
            r = g[f].rank(pct=True)
            X[f] = ((r - r.mean()) / r.std()).fillna(0.0) if r.notna().sum() > 10 else 0.0
        y = g[ret] - g[ret].mean()
        Xc = sm.add_constant(X.values, has_constant="add")
        try:
            b = np.linalg.lstsq(Xc, y.values, rcond=None)[0]
        except np.linalg.LinAlgError:
            continue
        coefs.append(pd.Series(b[1:], index=features, name=d))
    cf = pd.DataFrame(coefs)
    rows = []
    for f in features:
        m, t, pv = nw_tstat(cf[f], _lags(h))
        rows.append({"feature": f, "coef_mean": m, "t_nw": t, "p": pv, "n_months": cf[f].notna().sum(),
                     "coverage": float(p[f].notna().mean())})
    return pd.DataFrame(rows)


def top_vs_universe_hit_rates(panel: pd.DataFrame, score: str, h: int, top_n: int = 20) -> dict:
    p = panel[panel.eligible & panel[score].notna() & panel[f"fwd_{h}m"].notna()]
    top = p.sort_values(["date", score], ascending=[True, False]).groupby("date").head(top_n)
    return {
        "signal": score, "horizon_m": h,
        "top_win_rate_abs": float((top[f"fwd_{h}m"] > 0).mean()),
        "top_beat_nifty500": float((top[f"xs_{h}m"] > 0).mean()),
        "universe_beat_nifty500": float((p[f"xs_{h}m"] > 0).mean()),
        "top_mean_fwd": float(top[f"fwd_{h}m"].mean()), "top_median_fwd": float(top[f"fwd_{h}m"].median()),
        "universe_mean_fwd": float(p[f"fwd_{h}m"].mean()), "universe_median_fwd": float(p[f"fwd_{h}m"].median()),
        "top_mean_xs": float(top[f"xs_{h}m"].mean()), "top_median_xs": float(top[f"xs_{h}m"].median()),
        "n_obs": int(len(top)),
    }
