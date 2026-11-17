# Institutional Alpha — NSE quantitative research & backtest

An empirical test of the hypothesis that **Indian stocks under sustained accumulation by credible
institutions, combined with improving fundamentals and reasonable valuation, outperform over the
next 3–12 months.** It covers data acquisition from public exchange archives, point-in-time
reconstruction, five competing strategies, a backtest net of costs, statistical tests and three
case studies.

**Start here:** [`outputs/reports/Institutional_Alpha_Research_Report.md`](outputs/reports/Institutional_Alpha_Research_Report.md)
· interactive [`outputs/reports/dashboard.html`](outputs/reports/dashboard.html)
· [`outputs/reports/Data_Sources_and_Quality.md`](outputs/reports/Data_Sources_and_Quality.md)
· case studies in [`outputs/case_studies/`](outputs/case_studies/)

## Headline results (Jul-2018 → Oct-2026, 99 months, net of costs)

| Top-20, monthly | CAGR | vs Nifty 500 TRI (t) | vs equal-weight universe (t) | Max DD |
|---|---|---|---|---|
| A — block-absorption proxy | 10.7% | −1.3% (0.0) | −7.1% (−1.9) | −46% |
| B — shareholding accumulation | 15.7% | +3.7% (1.0) | −2.1% (−0.4) | −47% |
| C — B + earnings | 21.4% | +9.4% (2.1) | +3.6% (1.1) | −44% |
| D — A + B + earnings | 18.5% | +6.5% (1.6) | +0.7% (0.3) | −40% |
| E — full 100-point model | 20.3% | +8.3% (2.3) | +2.5% (0.3) | −33% |
| FII increase alone | 5.3% | −6.7% (−0.8) | −12.5% (−2.9) | −57% |
| Earnings-announcement reaction alone | 26.3% | +14.3% (2.6) | +8.5% (2.1) | −37% |
| Nifty 500 TRI / equal-weight universe | 12.0% / 17.8% | | | |

Quarterly institutional accumulation (FII, total institutions, named long-only buyers, absorbed
large-holder exits) shows little or no predictive power once filings are used at their publication
dates; MF increases carry a small edge that fades within a quarter. The composite models work mainly
through earnings-announcement reactions, momentum and low volatility, are concentrated in mid/small
caps, do not beat an equal-weighted universe significantly, and lose most of their edge if entry is
delayed by a month. Details and caveats: the research report.

## What data was actually used

| Data | Source | Period | Notes |
|---|---|---|---|
| Daily OHLCV, prev-close (adjusted base), ISIN | NSE bhavcopy archive (`nsearchives.nseindia.com`) | 2012-01 → 2026-11-13 | legacy + UDiFF formats; mainboard EQ/BE/BZ |
| Delivery volume | NSE MTO files | 2012 → 2026 | security-wise deliverable quantity |
| Corporate actions (dividends, splits, bonus) & board meetings | NSE daily PR bundles (Bc/Bm files) | 2012 → 2026 | dividends credited on ex-date; results dates |
| Index closes + dividend yield | NSE `ind_close_all` files | 2012-06 → 2026 | TRI approximated from published dividend yield |
| Quarterly shareholding (category totals + named >1% holders) | NSE SHP XBRL filings, URLs from the CC0 `aditya-jha/nse-historical-membership` filings index | Dec-2017 → Sep-2026 | parsed independently; FII/MF/insurance/etc. separated |
| PIT index membership, sector labels | same repo + NSE index constituent lists | 2014 → 2026 | used for coverage checks and sectors |
| Annual fundamentals | Yahoo Finance fundamentals-timeseries (**supplementary**) | FY2023 → FY2026 | only accessible fundamentals source; used from mid-2024 |
| Block-deal details for case studies | News reports (cited) | 2024–2026 | NSE's historical deal endpoint was not reachable |

**Not available in this environment (documented, not fabricated):** NSE/BSE historical bulk & block
deal files, primary-source quarterly results XBRL (discovery requires www.nseindia.com /
api.bseindia.com, which refuse this environment), official TRI series, and shareholding XBRL for
companies delisted before 2026. See the report's *Limitations* section for how each gap is handled.

## Reproduce

```bash
pip install -r requirements.txt
python run_pipeline.py                  # downloads (~3h first run, resumable) + full research
python run_pipeline.py --skip-download  # rebuild from data/raw
python -m pytest                        # unit tests (PIT timing, parsers, classifier, portfolio mechanics)
```

Raw downloads (~2 GB) and large Parquet files are git-ignored; everything is rebuilt by the
pipeline. `data/processed/institutional_alpha.duckdb` exposes all processed Parquet files as DuckDB
views.

## Layout

```
config/            settings.yaml (universe, costs, splits), scoring_weights.yaml, investor_classification.csv
data/reference/    case_study_events.csv (news-verified events with sources)
data/metadata/     datasets.json (source URL, retrieval date, coverage, records, quality, limitations)
src/
  data_acquisition/       nse_archives.py, shp_xbrl.py, yahoo_fundamentals.py
  data_cleaning/          prices.py, indices.py, shareholding.py, warehouse.py
  investor_classification/classifier.py
  feature_engineering/    features.py  (PIT monthly panel, forward returns)
  screening/              scoring.py   (strategies A–E, 100-point model)
  portfolio/              construction.py
  backtesting/            engine.py, walk_forward.py
  performance/            metrics.py, predictive.py
  reporting/              research.py, case_studies.py, dashboard.py, report.py
notebooks/         01–05 executed notebooks (+ build_notebooks.py)
tests/             pytest suite
outputs/           charts/, tables/, case_studies/, reports/
run_pipeline.py
```

## Point-in-time rules (summary)

* A shareholding filing is usable from the first session **strictly after** its public broadcast
  (revised filings: original submission date).
* Signals are formed at each month-end close; trades execute at the **next session's close**.
* Results-based features use NSE board-meeting dates; accounting data use the results date (or the
  SEBI deadline) of each fiscal year.
* The universe is rebuilt every month from stocks that actually traded, ranked by market cap
  computed from filed share counts — never today's index list.

Not investment advice.
