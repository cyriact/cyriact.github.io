# Data sources, coverage and quality

Generated from `data/metadata/datasets.json` by `src/reporting/report.py`.

| dataset | source | coverage | records | quality score | known limitations |
|---|---|---|---|---|---|
| board_meetings | PR*.zip (Bm files) | 2012-01-04 → 2026-12-05 | 120682 | 0.8 | Scheduled board-meeting dates (results announcements) as disseminated by NSE. |
| corporate_actions | https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR*.zip (Bc files) | 2012-01-03 → 2026-11-25 | 67224 | 0.85 | Dividend amounts and split/bonus/consolidation factors parsed from free-text PURPOSE; factors validated against ex-date price jumps; rights issues not adjusted; demergers neutralised. |
| fundamentals_yahoo | https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/ | 2021-12-31 → 2026-09-30 | 186135 | 0.6 | SUPPLEMENTARY. ~4 annual periods (FY2023-FY2026) + ~5 quarters; latest restated values (not as-first-reported); current listings only (survivorship). Availability dates are imputed from NSE board-meeting dates or statutory deadlines. |
| index_levels | https://nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv | 2012-06-01 → 2026-11-13 | 22050 | 0.9 | TRI approximated from price index + NSE published dividend yield (not official TRI). Midcap 150 / Smallcap 250 / Microcap 250 histories start when NSE began publishing them in this file. |
| monthly_panel | derived | 2017-12-29 → 2026-11-13 | 261207 | 0.85 | Universe limited to symbols with SHP filings (survivorship gap for delisted names). |
| prices_daily | https://nsearchives.nseindia.com/content/historical/EQUITIES/ ; /content/cm/ ; /archives/equities/mto/ | 2012-01-02 → 2026-11-13 | 6242820 | 0.95 | Mainboard EQ/BE/BZ only. Dividends credited from parsed corporate-action text; rare unparsed purposes are missed. Delisted names keep their history until last trade. |
| shareholding_quarterly | derived from shp_xbrl | 2018-03-31 → 2026-09-30 | 31178 | 0.85 | Category totals per SEBI SHP format; FII = FPI cat I+II (+ foreign SWF in 2022+ taxonomy). |
| shp_xbrl | https://nsearchives.nseindia.com/corporate/xbrl/ (URLs from aditya-jha/nse-historical-membership filings_index) | 2018-01-19 → 2026-10-27 | 32999 | 0.85 | Filings index only covers symbols listed in 2026 (delisted/merged companies such as HDFC, DHFL are absent: survivorship gap). XBRL exists from ~Dec-2017 only. Named holders are only those >1% (SEBI disclosure threshold). |

## Validation checks

| check                                                      | result                                                | detail                                                                                                                                                          |
|:-----------------------------------------------------------|:------------------------------------------------------|:----------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Trading sessions captured                                  | 3,686                                                 | 2012-01-02 to 2026-11-13; 244-252 per full calendar year                                                                                                        |
| NSE PREVCLOSE adjusted for dividends?                      | No                                                    | 99.9% of 2,860 ex-dates with yield >2% have PREVCLOSE = prior close -> dividends added explicitly                                                               |
| Split/bonus/consolidation factors applied                  | 953                                                   | from NSE corporate-action records, validated against the ex-date price jump                                                                                     |
| Split-like jumps inferred (no record)                      | 415                                                   | flagged ca_inferred                                                                                                                                             |
| Demerger ex-dates neutralised                              | 108                                                   | ex-date return set to 0 (value distributed)                                                                                                                     |
| Dividend events credited                                   | 18,679                                                | cash dividends on ex-date                                                                                                                                       |
| Remaining |daily return| > 50% with traded value > Rs 1 cr | 19                                                    | AMTEKAUTO 2015-09-11, BCG 2025-07-14, CCAVENUE 2018-09-28, CCCL 2024-09-03, COFFEEDAY 2021-04-26, COREEDUTEC 2013-02-25, INFY 2014-12-02, JETAIRWAYS 2019-06-20 |
| Delivery data coverage                                     | 90.2%                                                 | share of price rows with MTO delivery %                                                                                                                         |
| SHP parser vs external repo parse (promoter %)             | 100.00% within 0.05pp                                 | 32,682 overlapping filings                                                                                                                                      |
| SHP parser vs external repo (reported FII+DII %)           | 100.00% within 0.05pp                                 | 2022+ filings where the repo has FII/DII                                                                                                                        |
| External repo's own check vs StockEdge (±0.5pp)            | promoter 99.9%, fii 98.09%, dii 94.83%, public 93.95% | 6,922 symbol-quarters                                                                                                                                           |
| PIT index membership: current Nifty 500 vs NSE list        | 499/501 match                                         | only in NSE list: ['DUMMYHEG', 'MSUMI']; only in repo: []                                                                                                       |
| SHP XBRL filings parsed                                    | 32,992                                                | 1,122 symbols; 72 URLs returned 404 on the archive                                                                                                              |

## Universe coverage (month-end snapshots)

- Months: 108; eligible stocks per month: median 750 (min 0, max 750).
- Share of point-in-time Nifty 500 constituents (that traded) with shareholding coverage: median 96.6%, min 0.0%.
- Eligible stocks with a fresh institutional-change signal: median 722.

Retrieval dates are recorded per dataset in `data/metadata/datasets.json`.