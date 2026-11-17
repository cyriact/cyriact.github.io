"""Generate and execute the five research notebooks (run from the project root):

    python notebooks/build_notebooks.py
"""
from __future__ import annotations

from pathlib import Path

import nbformat as nbf
from nbconvert.preprocessors import ExecutePreprocessor

HERE = Path(__file__).resolve().parent
SETUP = """import os, sys
from pathlib import Path
ROOT = Path.cwd().parent if Path.cwd().name == 'notebooks' else Path.cwd()
os.chdir(ROOT); sys.path.insert(0, str(ROOT))
import pandas as pd, numpy as np, matplotlib.pyplot as plt
pd.set_option('display.width', 200); pd.set_option('display.max_columns', 40)
P = ROOT / 'data' / 'processed'; T = ROOT / 'outputs' / 'tables'"""

NB = {
    "01_data_exploration.ipynb": [
        ("md", "# 01 · Data exploration\nCoverage and sanity checks of the NSE archive data (prices, delivery, corporate actions, indices) and the SHP XBRL shareholding filings. Every dataset's provenance is in `data/metadata/datasets.json`."),
        ("code", SETUP),
        ("code", "import json\ncat = json.loads((ROOT/'data/metadata/datasets.json').read_text())\npd.DataFrame(cat).T[['source_url','coverage_start','coverage_end','n_records','quality_score']]"),
        ("code", "px = pd.read_parquet(P/'prices_daily.parquet', columns=['symbol','date','close','value_cr','deliv_pct','ca_flag','dps'])\nprint(px.shape, px.symbol.nunique(), px.date.min().date(), px.date.max().date())\npx.groupby(px.date.dt.year).agg(sessions=('date','nunique'), symbols=('symbol','nunique'), ca_events=('ca_flag','sum'), dividend_events=('dps', lambda s:(s>0).sum()))"),
        ("code", "cov = pd.read_csv(T/'universe_coverage.csv', parse_dates=['date'])\nax = cov.set_index('date')[['n_large','n_mid','n_small']].plot.area(figsize=(9,3.5), title='Eligible universe by segment (PIT)'); plt.show()\ncov.set_index('date')['nifty500_coverage'].plot(figsize=(9,2.5), title='Share of PIT Nifty 500 constituents with SHP coverage'); plt.show()"),
        ("code", "idx = pd.read_parquet(P/'index_levels.parquet')\nidx.pivot(index='date', columns='index', values='tri').resample('ME').last().plot(logy=True, figsize=(9,4), title='Approximate TRI (price index + published dividend yield)'); plt.show()"),
    ],
    "02_institutional_signals.ipynb": [
        ("md", "# 02 · Institutional signals\nQuarterly ownership changes (FII = FPI cat I+II, MF, insurance, total institutions), named >1% holders classified by investor type, and large-holder exits absorbed by institutions."),
        ("code", SETUP),
        ("code", "q = pd.read_parquet(P/'shareholding_quarterly.parquet')\nq[['d_fii_1q','d_mf_1q','d_insurance_1q','d_inst_1q','flow_inst_1q','share_change_ex_split']].describe().T"),
        ("code", "obs = pd.read_csv(P/'investor_names_observed.csv')\nobs.groupby('investor_category').agg(names=('original_name','nunique'), filings=('n_filings','sum')).sort_values('filings', ascending=False)"),
        ("code", "pd.read_csv(T/'ic_summary.csv').query('signal.str.contains(\"FII|MF|INST|INSURANCE|named|delivery|absorption\")', engine='python').round(3)"),
        ("code", "pd.read_csv(T/'absorption_event_study.csv').round(4)"),
        ("code", "fm = pd.read_csv(T/'fama_macbeth.csv'); fm[fm.horizon==3].round(4)"),
    ],
    "03_earnings_features.ipynb": [
        ("md", "# 03 · Earnings features\nFull-period earnings proxy: 3-session abnormal return around results board meetings (NSE Bm files). Accounting growth/margins from Yahoo annual data (supplementary, FY2023-FY2026, used only from mid-2024)."),
        ("code", SETUP),
        ("code", "ear = pd.read_parquet(P/'earnings_announcement_returns.parquet')\nprint(len(ear), ear.symbol.nunique()); ear.ear.describe()"),
        ("code", "af = pd.read_parquet(P/'accounting_features.parquet')\naf[['rev_g','ebitda_g','pat_g','margin_chg','roce','ocf_conv']].describe().T.round(3)"),
        ("code", "pd.read_csv(T/'accounting_window_performance.csv').query('benchmark==\"Nifty 500\"')[['strategy','months','CAGR','bench_CAGR','excess_CAGR','t_excess_nw','sharpe','max_dd']].round(3)"),
    ],
    "04_strategy_comparison.ipynb": [
        ("md", "# 04 · Strategy comparison\nStrategies A-E, single-signal portfolios, alternative weightings, segments, variants and walk-forward weights."),
        ("code", SETUP),
        ("code", "perf = pd.read_csv(T/'strategy_performance.csv')\nperf[perf.benchmark=='Nifty 500'][['strategy','CAGR','bench_CAGR','excess_CAGR','excess_CAGR_ci_lo','excess_CAGR_ci_hi','t_excess_nw','sharpe','max_dd','avg_turnover_m']].round(3)"),
        ("code", "m = pd.read_csv(T/'monthly_returns_net.csv', index_col=0, parse_dates=True)\n(1+m[['A_block','B_shareholding','C_inst_earnings','D_inst_block_earnings','E_full','Nifty 500','Universe EW']].fillna(0)).cumprod().plot(logy=True, figsize=(9,4.5)); plt.show()"),
        ("code", "pd.read_csv(T/'subperiod_performance.csv').round(3)"),
        ("code", "pd.read_csv(T/'regime_performance.csv').round(4)"),
        ("code", "pd.read_csv(T/'concentration_extreme_winners.csv').round(3)"),
        ("code", "pd.read_csv(T/'walk_forward_weights.csv')"),
    ],
    "05_case_studies.ipynb": [
        ("md", "# 05 · Case studies\nSAILIFE (2025), ETERNAL/ZOMATO (Jul-2024) and MEESHO (Nov-2026, prospective). Full write-ups: `outputs/case_studies/*.md`."),
        ("code", SETUP),
        ("code", "pd.read_csv(ROOT/'data/reference/case_study_events.csv')[['case','event_date','event_type','description','verification_status']]"),
        ("code", "pd.read_csv(ROOT/'outputs/case_studies/case_study_entry_returns.csv').round(3)"),
        ("code", "from IPython.display import Image, display\nfor s in ['SAILIFE','ETERNAL','MEESHO']:\n    f = ROOT/'outputs/case_studies'/f'{s}_chart.png'\n    if f.exists(): display(Image(filename=str(f)))"),
    ],
}


def main() -> None:
    ep = ExecutePreprocessor(timeout=600, kernel_name="python3")
    for name, cells in NB.items():
        nb = nbf.v4.new_notebook()
        nb.cells = [nbf.v4.new_markdown_cell(c) if k == "md" else nbf.v4.new_code_cell(c) for k, c in cells]
        ep.preprocess(nb, {"metadata": {"path": str(HERE)}})
        nbf.write(nb, HERE / name)
        print("wrote", name)


if __name__ == "__main__":
    main()
