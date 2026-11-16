"""Performance, risk and statistical-significance metrics for monthly return series."""
from __future__ import annotations

import numpy as np
import pandas as pd
import statsmodels.api as sm

RF_ANNUAL = 0.06  # constant INR risk-free proxy (91-day T-bill averaged ~5-7% over 2018-2026)
RF_M = (1 + RF_ANNUAL) ** (1 / 12) - 1


def cagr(r: pd.Series) -> float:
    r = r.dropna()
    if len(r) == 0:
        return np.nan
    return float((1 + r).prod() ** (12 / len(r)) - 1)


def max_drawdown(r: pd.Series) -> float:
    nav = (1 + r.fillna(0)).cumprod()
    return float((nav / nav.cummax() - 1).min())


def drawdown_series(r: pd.Series) -> pd.Series:
    nav = (1 + r.fillna(0)).cumprod()
    return nav / nav.cummax() - 1


def nw_tstat(x: pd.Series, lags: int = 3) -> tuple[float, float, float]:
    """Mean, Newey-West t-stat and p-value of a series (HAC, Bartlett kernel)."""
    x = pd.Series(x).dropna()
    if len(x) < 6:
        return np.nan, np.nan, np.nan
    res = sm.OLS(x.values, np.ones(len(x))).fit(cov_type="HAC", cov_kwds={"maxlags": lags})
    return float(res.params[0]), float(res.tvalues[0]), float(res.pvalues[0])


def stationary_bootstrap(x: np.ndarray, n_boot: int = 2000, mean_block: int = 6, seed: int = 7) -> np.ndarray:
    """Politis-Romano stationary bootstrap indices -> resampled series matrix (n_boot x T)."""
    rng = np.random.default_rng(seed)
    T = len(x)
    p = 1.0 / mean_block
    idx = np.empty((n_boot, T), dtype=int)
    idx[:, 0] = rng.integers(0, T, n_boot)
    for t in range(1, T):
        new = rng.random(n_boot) < p
        idx[:, t] = np.where(new, rng.integers(0, T, n_boot), (idx[:, t - 1] + 1) % T)
    return x[idx]


def perf_stats(r: pd.Series, b: pd.Series | None = None, label: str = "", n_boot: int = 2000) -> dict:
    r = r.dropna()
    out = {"strategy": label, "start": r.index.min(), "end": r.index.max(), "months": len(r)}
    if len(r) < 3:
        return out
    ex_rf = r - RF_M
    vol = r.std() * np.sqrt(12)
    down = ex_rf[ex_rf < 0]
    out.update({
        "CAGR": cagr(r), "ann_vol": vol,
        "sharpe": ex_rf.mean() / r.std() * np.sqrt(12) if r.std() > 0 else np.nan,
        "sortino": ex_rf.mean() * 12 / (np.sqrt((down ** 2).sum() / len(ex_rf)) * np.sqrt(12)) if len(down) else np.nan,
        "max_dd": max_drawdown(r), "worst_month": r.min(), "best_month": r.max(),
        "worst_12m": (1 + r).rolling(12).apply(np.prod, raw=True).min() - 1 if len(r) >= 12 else np.nan,
        "pct_pos_months": (r > 0).mean(),
    })
    out["calmar"] = out["CAGR"] / abs(out["max_dd"]) if out["max_dd"] < 0 else np.nan
    if b is not None:
        b = b.reindex(r.index)
        ok = b.notna()
        if ok.sum() < 6:
            return out
        ex = (r - b)[ok]
        out.update({
            "bench_CAGR": cagr(b[ok]), "excess_CAGR": cagr(r[ok]) - cagr(b[ok]),
            "tracking_error": ex.std() * np.sqrt(12),
            "info_ratio": ex.mean() / ex.std() * np.sqrt(12) if ex.std() > 0 else np.nan,
            "hit_rate_vs_bench": (ex > 0).mean(),
        })
        m, t, p = nw_tstat(ex, lags=3)
        out.update({"mean_excess_m": m, "t_excess_nw": t, "p_excess_nw": p})
        X = sm.add_constant((b[ok] - RF_M).values)
        reg = sm.OLS((r[ok] - RF_M).values, X).fit(cov_type="HAC", cov_kwds={"maxlags": 3})
        out.update({"alpha_ann": float(reg.params[0] * 12), "alpha_t": float(reg.tvalues[0]), "beta": float(reg.params[1])})
        if n_boot and len(ex) >= 24:
            bs = stationary_bootstrap(np.column_stack([r[ok].values, b[ok].values]), n_boot=n_boot)
            rr, bb = bs[..., 0], bs[..., 1]
            T = rr.shape[1]
            cg = np.prod(1 + rr, axis=1) ** (12 / T) - np.prod(1 + bb, axis=1) ** (12 / T)
            sh = (rr.mean(1) - RF_M) / rr.std(1) * np.sqrt(12)
            out.update({"excess_CAGR_ci_lo": float(np.percentile(cg, 2.5)), "excess_CAGR_ci_hi": float(np.percentile(cg, 97.5)),
                        "sharpe_ci_lo": float(np.percentile(sh, 2.5)), "sharpe_ci_hi": float(np.percentile(sh, 97.5)),
                        "p_excess_le0_boot": float((cg <= 0).mean())})
    return out


def annual_returns(df: pd.DataFrame) -> pd.DataFrame:
    """Calendar-year compounded returns for every column of a monthly-returns frame."""
    y = df.groupby(df.index.year).apply(lambda g: (1 + g).prod(min_count=1) - 1)
    y.index.name = "year"
    return y


def rolling_12m(r: pd.Series) -> pd.Series:
    return (1 + r).rolling(12).apply(np.prod, raw=True) - 1
