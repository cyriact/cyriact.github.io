"""Portfolio construction: top-N selection, equal / score weights with a per-stock cap."""
from __future__ import annotations

import numpy as np
import pandas as pd


def cap_weights(w: pd.Series, cap: float) -> pd.Series:
    """Iteratively cap weights at `cap`, redistributing the excess pro-rata (leftover = cash)."""
    w = w.clip(lower=0).astype(float)
    if w.sum() <= 0:
        return w
    w = w / w.sum()
    for _ in range(50):
        over = w > cap + 1e-12
        if not over.any():
            break
        excess = (w[over] - cap).sum()
        w[over] = cap
        under = ~over & (w > 0)
        if not under.any() or w[under].sum() <= 0:
            break
        w[under] += excess * w[under] / w[under].sum()
    return w.clip(upper=cap)


def select(cands: pd.DataFrame, score_col: str, top_n: int, weighting: str = "equal",
           max_weight: float = 0.05) -> pd.Series:
    """Weights for one rebalance date. cands must be eligible rows with a non-null score.
    Ties broken by higher liquidity (adtv) then symbol for reproducibility."""
    c = cands.dropna(subset=[score_col])
    if c.empty:
        return pd.Series(dtype=float)
    c = c.sort_values([score_col, "adtv_63_cr", "symbol"], ascending=[False, False, True]).head(top_n)
    if weighting == "score":
        raw = c[score_col] - c[score_col].min() + 1.0
    else:
        raw = pd.Series(1.0, index=c.index)
    cap = max(max_weight, 1.0 / top_n) if weighting == "equal" else max_weight
    w = cap_weights(raw, cap)
    w.index = c["symbol"].values
    return w
