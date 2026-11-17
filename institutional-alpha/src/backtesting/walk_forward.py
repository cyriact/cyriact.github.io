"""Walk-forward optimisation of the 5 component weights (no future information).

Grid: weights in steps of 10 summing to 100 across the 5 components (1,001 combinations).
Objective on the training window: mean monthly excess return of the top-N names (by composite) over
the equal-weighted candidate universe, using only months whose 1-month forward return was fully
realised before the training cut-off. Re-estimated every January on an expanding window; the chosen
weights are applied to the following 12 signal dates only.
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

COMPS = ["earnings", "institutional", "quality", "valuation", "price_volume"]


def weight_grid(step: int = 10) -> np.ndarray:
    vals = range(0, 101, step)
    combos = [c for c in itertools.product(vals, repeat=len(COMPS)) if sum(c) == 100]
    return np.array(combos, dtype=float)


def per_date_objective(cand: pd.DataFrame, W: np.ndarray, top_n: int = 20) -> pd.DataFrame:
    """Rows: dates; cols: combo index -> top-N mean fwd_1m minus candidate mean fwd_1m."""
    out = {}
    for d, g in cand.groupby("date"):
        g = g.dropna(subset=["fwd_1m"])
        if len(g) < top_n * 3:
            continue
        X = g[[f"c_{c}" for c in COMPS]].to_numpy()
        S = X @ W.T  # n x combos
        r = g["fwd_1m"].to_numpy()
        idx = np.argpartition(-S, top_n - 1, axis=0)[:top_n]  # top_n x combos
        top_mean = r[idx].mean(axis=0)
        out[d] = top_mean - r.mean()
    return pd.DataFrame(out).T.sort_index()


def run(cand: pd.DataFrame, top_n: int = 20, first_year: int = 2020, min_train_months: int = 18,
        base: tuple = (30, 25, 20, 20, 5)) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    W = weight_grid()
    obj = per_date_objective(cand, W, top_n)
    exit_map = cand.drop_duplicates("date").set_index("date")["exit_1m"]
    base_i = int(np.argmin(np.abs(W - np.array(base)).sum(1)))
    chosen_rows = []
    score_parts = []
    years = sorted({d.year for d in cand.date.unique() if d.year >= first_year})
    for y in years:
        cutoff = pd.Timestamp(f"{y}-01-01")
        train_dates = [d for d in obj.index if pd.notna(exit_map.get(d)) and exit_map[d] < cutoff]
        if len(train_dates) < min_train_months:
            continue
        m = obj.loc[train_dates].mean()
        # prefer simpler / closer-to-base weights on near-ties (within 2bp/month)
        best = m.max()
        near = m[m >= best - 0.0002].index
        dist = np.abs(W[near] - np.array(base)).sum(1)
        bi = int(near[np.argmin(dist)])
        w = dict(zip(COMPS, W[bi]))
        chosen_rows.append({"year": y, "train_start": min(train_dates), "train_end": max(train_dates),
                            "n_train_months": len(train_dates), **{f"w_{k}": v for k, v in w.items()},
                            "train_obj_bp": best * 1e4, "base_train_obj_bp": m.iloc[base_i] * 1e4})
        sel = cand[cand.date.dt.year == y]
        s = sum(sel[f"c_{c}"] * w[c] for c in COMPS)
        score_parts.append(s)
    chosen = pd.DataFrame(chosen_rows)
    score = pd.concat(score_parts) if score_parts else pd.Series(dtype=float)
    # in-sample (look-ahead) optimum for reference only
    full = obj.mean()
    ins = pd.DataFrame([{**dict(zip(COMPS, W[int(full.idxmax())])), "obj_bp": full.max() * 1e4,
                         "base_obj_bp": full.iloc[base_i] * 1e4}])
    return chosen, score.rename("score_WF"), ins
