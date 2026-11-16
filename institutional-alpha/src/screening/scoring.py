"""Reproducible, rule-based scores (cross-sectional percentile ranks within the eligible universe)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.common import load_yaml

CFG = load_yaml("scoring_weights.yaml")
COMPONENTS = list(CFG["components"].keys())


def pct_rank(panel: pd.DataFrame, col: str, direction: int) -> pd.Series:
    """Percentile rank within date among eligible rows (NaN stays NaN). Ties averaged."""
    s = panel[col].astype(float) * direction
    r = s.groupby(panel["date"]).rank(pct=True, method="average")
    return r


def feature_ranks(panel: pd.DataFrame, features: dict[str, int]) -> pd.DataFrame:
    cols = {}
    for f, d in features.items():
        if f in panel.columns:
            cols[f] = pct_rank(panel, f, d)
    return pd.DataFrame(cols, index=panel.index)


def component_scores(panel: pd.DataFrame) -> pd.DataFrame:
    """Each component = mean of available sub-feature percentiles (0.5 if none available)."""
    out = {}
    cov = {}
    for comp, feats in CFG["components"].items():
        fr = feature_ranks(panel, feats)
        out[f"c_{comp}"] = fr.mean(axis=1).fillna(0.5)
        cov[f"cov_{comp}"] = fr.notna().sum(axis=1)
    return pd.concat([pd.DataFrame(out), pd.DataFrame(cov)], axis=1)


def weighted_score(comp: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    tot = sum(weights.values())
    s = sum(comp[f"c_{k}"] * w for k, w in weights.items()) / tot * 100
    return s


def strategy_scores(panel: pd.DataFrame) -> pd.DataFrame:
    """Return a frame (aligned with panel) with score_<strategy> and component columns.

    Ranks are computed among `eligible` rows only so that the cross-section is the PIT universe.
    """
    elig = panel[panel["eligible"]].copy()
    comp = component_scores(elig)
    res = pd.DataFrame(index=elig.index)
    res = pd.concat([res, comp], axis=1)
    strat = CFG["strategies"]

    def has_any(cols):
        present = [c for c in cols if c in elig.columns]
        return elig[present].notna().any(axis=1) if present else pd.Series(False, index=elig.index)

    # A and B: mean of their feature percentiles
    for name in ("A_block", "B_shareholding"):
        fr = feature_ranks(elig, strat[name]["features"])
        sc = fr.mean(axis=1) * 100
        sc[~has_any(strat[name]["require_any"])] = np.nan
        res[f"score_{name}"] = sc
    earn = comp["c_earnings"] * 100
    res["score_earnings"] = earn
    for name in ("C_inst_earnings", "D_inst_block_earnings"):
        blend = strat[name]["blend"]
        parts = []
        for k, w in blend.items():
            base = res[f"score_{k}"] if f"score_{k}" in res else earn
            parts.append(base.fillna(50) * w if k == "A_block" else base * w)
        sc = sum(parts) / sum(blend.values())
        sc[~has_any(strat[name]["require_any"])] = np.nan
        res[f"score_{name}"] = sc
    e = strat["E_full"]
    sc = weighted_score(comp, CFG["weights"][e["weights"]])
    sc[~has_any(e["require_any"]) | (elig["adtv_63_cr"] < e["min_adtv_cr"])] = np.nan
    res["score_E_full"] = sc
    # alternative weightings of the full model
    for wname, w in CFG["weights"].items():
        s2 = weighted_score(comp, w)
        s2[~has_any(e["require_any"]) | (elig["adtv_63_cr"] < e["min_adtv_cr"])] = np.nan
        res[f"score_W_{wname}"] = s2
    return res.reindex(panel.index)
