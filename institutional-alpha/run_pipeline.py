#!/usr/bin/env python
"""Institutional Alpha — end-to-end research pipeline.

    python run_pipeline.py                 # everything (downloads are resumable; ~3h first run)
    python run_pipeline.py --skip-download # rebuild from data/raw
    python run_pipeline.py --steps research,case_studies,report

Steps: download -> clean -> shareholding -> features -> research -> case_studies -> report
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

import numpy as np
import pandas as pd

from src.common import ROOT, get_logger, path

log = get_logger("pipeline")
ALL = ["download", "clean", "shareholding", "features", "research", "case_studies", "report"]


def ensure_external_repo() -> None:
    dest = path("raw") / "external" / "nse-historical-membership"
    if not dest.exists():
        from src.common import SETTINGS
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", SETTINGS["acquisition"]["external_repo_url"], str(dest)], check=True)


def step_download() -> None:
    ensure_external_repo()
    from src.data_acquisition import nse_archives, shp_xbrl, yahoo_fundamentals
    import datetime as dt
    from src.common import SETTINGS
    a = SETTINGS["acquisition"]
    nse_archives.run(["bhavcopy", "mto", "pr", "indices"], dt.date.fromisoformat(a["start_date"]),
                     dt.date.fromisoformat(a["end_date"]), a["workers"])
    syms = shp_xbrl.default_symbols()
    fil = shp_xbrl.load_filings_index(syms, a["shp_first_period"])
    shp_xbrl.download(fil, workers=4)
    shp_xbrl.parse_all(fil)
    yahoo_fundamentals.run(sorted(syms))


def step_clean() -> None:
    from src.data_cleaning import indices, prices
    prices.build()
    indices.build()


def _calendar() -> pd.DatetimeIndex:
    px = pd.read_parquet(path("processed") / "prices_daily.parquet", columns=["date"])
    return pd.DatetimeIndex(np.sort(px.date.unique()))


def step_shareholding() -> None:
    from src.data_acquisition import shp_xbrl
    from src.data_cleaning import shareholding
    fp = path("processed") / "shp_filings.parquet"
    if not fp.exists():
        syms = shp_xbrl.default_symbols()
        shp_xbrl.parse_all(shp_xbrl.load_filings_index(syms, "2017-09"))
    px = pd.read_parquet(path("processed") / "prices_daily.parquet", columns=["symbol", "date", "ca_flag", "ca_ratio"])
    q = shareholding.build(_calendar(), px)
    sp = pd.read_parquet(path("processed") / "shareholding_filings_pit.parquet")
    fam = shareholding.build_named_holders(sp)
    shareholding.named_holder_flows(fam, q)


def step_features() -> None:
    from src.feature_engineering import features as F
    proc = path("processed")
    px = pd.read_parquet(proc / "prices_daily.parquet")
    idx = pd.read_parquet(proc / "index_levels.parquet")
    q = pd.read_parquet(proc / "shareholding_quarterly.parquet")
    nhf = pd.read_parquet(proc / "named_holder_flows.parquet")
    bm = pd.read_parquet(proc / "board_meetings.parquet")
    fund = pd.read_parquet(proc / "fundamentals_yahoo.parquet") if (proc / "fundamentals_yahoo.parquet").exists() else None
    F.build_panel(px, idx, q, nhf, bm, fund, F.load_sectors())


def step_research() -> None:
    from src.reporting import research
    proc = path("processed")
    panel = pd.read_parquet(proc / "monthly_panel.parquet")
    idx = pd.read_parquet(proc / "index_levels.parquet")
    research.run_research(panel, idx)


def step_case_studies() -> None:
    from src.reporting import case_studies
    proc = path("processed")
    panel = pd.read_parquet(proc / "panel_scored.parquet")
    px = pd.read_parquet(proc / "prices_daily.parquet")
    idx = pd.read_parquet(proc / "index_levels.parquet")
    case_studies.run(panel, px, idx)


def step_report() -> None:
    from src.data_cleaning import warehouse
    from src.reporting import dashboard, report
    dashboard.build()
    report.build_data_appendix()
    warehouse.build()
    subprocess.run([sys.executable, str(ROOT / "notebooks" / "build_notebooks.py")], check=False, cwd=str(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default=",".join(ALL))
    ap.add_argument("--skip-download", action="store_true")
    a = ap.parse_args()
    steps = [s for s in a.steps.split(",") if s]
    if a.skip_download and "download" in steps:
        steps.remove("download")
    for s in steps:
        t0 = time.time()
        log.info("=== step %s ===", s)
        globals()[f"step_{s}"]()
        log.info("=== step %s done in %.0fs ===", s, time.time() - t0)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
