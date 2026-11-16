"""NSE index closing values (ind_close_all_*.csv) and approximate total-return indices.

NSE's archive publishes daily price-index closes together with the index dividend yield. The
official TRI series sit behind niftyindices.com session-protected endpoints that we do not use,
so we construct an approximate TRI:

    TRI_t = TRI_{t-1} * (P_t / P_{t-1} + DY_{t-1}/100 * days_t/365)

where DY is NSE's published trailing dividend yield and days_t the calendar days elapsed. This
reinvests the index's trailing dividends daily; it typically tracks the official TRI within a few
basis points per year (dividend timing is smoothed). Documented as an approximation.
"""
from __future__ import annotations

import glob

import numpy as np
import pandas as pd

from src.common import ensure_dir, get_logger, path, register_dataset

log = get_logger("indices")

BENCH = {
    "Nifty 50": ["Nifty 50", "S&P CNX Nifty", "CNX Nifty"],
    "Nifty 500": ["Nifty 500", "S&P CNX 500", "CNX 500"],
    "Nifty Midcap 150": ["Nifty Midcap 150", "NIFTY Midcap 150"],
    "Nifty Smallcap 250": ["Nifty Smallcap 250", "NIFTY Smallcap 250"],
    "Nifty Microcap 250": ["Nifty Microcap 250", "NIFTY Microcap 250"],
    "Nifty Midcap 100": ["Nifty Midcap 100", "CNX Midcap", "NIFTY Midcap 100"],
    "Nifty Smallcap 100": ["Nifty Smallcap 100", "CNX Smallcap", "NIFTY Smallcap 100"],
    "Nifty 50 Equal Weight": ["NIFTY50 Equal Weight", "NIFTY50 EQL Wgt", "Nifty50 Equal Weight"],
}


def build(write: bool = True) -> pd.DataFrame:
    files = sorted(glob.glob(str(path("raw") / "indices" / "*" / "ind_close_all_*.csv")))
    parts = []
    for fp in files:
        try:
            d = pd.read_csv(fp)
        except Exception:  # noqa: BLE001
            continue
        d.columns = [c.strip() for c in d.columns]
        parts.append(d)
    raw = pd.concat(parts, ignore_index=True)
    raw["Index Name"] = raw["Index Name"].astype(str).str.strip()
    raw["date"] = pd.to_datetime(raw["Index Date"], dayfirst=True, errors="coerce")
    alias = {a.lower(): k for k, v in BENCH.items() for a in v}
    raw["index"] = raw["Index Name"].str.lower().map(alias)
    lv = raw.dropna(subset=["index", "date"]).copy()
    lv = lv.rename(columns={"Closing Index Value": "close", "P/E": "pe", "P/B": "pb", "Div Yield": "dy"})
    for c in ("close", "pe", "pb", "dy"):
        lv[c] = pd.to_numeric(lv[c], errors="coerce")
    lv = lv.sort_values(["index", "date"]).drop_duplicates(["index", "date"], keep="last")
    out = []
    for name, g in lv.groupby("index"):
        g = g[["date", "close", "pe", "pb", "dy"]].dropna(subset=["close"]).copy()
        pr = g.close / g.close.shift(1)
        days = g.date.diff().dt.days
        dy = g.dy.shift(1).ffill().fillna(g.dy.median())
        g["ret_px"] = pr - 1
        g["ret_tr"] = pr - 1 + dy / 100 * days / 365
        g.loc[g.index[0], ["ret_px", "ret_tr"]] = 0.0
        g["tri"] = 1000 * (1 + g.ret_tr).cumprod()
        g["index"] = name
        out.append(g)
    res = pd.concat(out, ignore_index=True)
    if write:
        res.to_parquet(ensure_dir(path("processed")) / "index_levels.parquet", index=False)
        register_dataset(
            "index_levels", source_url="https://nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv",
            coverage_start=str(res.date.min().date()), coverage_end=str(res.date.max().date()),
            n_records=int(len(res)), quality_score=0.9,
            limitations="TRI approximated from price index + NSE published dividend yield (not official TRI). "
                        "Midcap 150 / Smallcap 250 / Microcap 250 histories start when NSE began publishing them in this file.",
            path="data/processed/index_levels.parquet")
        log.info("index levels: %s", res.groupby("index").date.agg(["min", "max", "count"]).to_string())
    return res


if __name__ == "__main__":
    build()
