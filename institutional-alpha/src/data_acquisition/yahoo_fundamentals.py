"""Supplementary fundamentals from Yahoo Finance's public fundamentals-timeseries endpoint.

Why supplementary: primary-source quarterly results (NSE/BSE XBRL) can only be *discovered* via
www.nseindia.com / api.bseindia.com endpoints that refuse this environment; we do not bypass that.
Yahoo returns ~4 fiscal years (FY2023-FY2026) of annual statements and ~5 recent quarters for
currently-listed NSE stocks. Values are Yahoo's latest (possibly restated) figures, not
as-first-reported, and delisted companies are absent. These limitations are documented and the
accounting-earnings strategies are therefore only evaluated from mid-2024 onward.

Output: data/processed/fundamentals_yahoo.parquet  (symbol, freq, field, period_end, value)
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

from src.common import ensure_dir, get_logger, path, register_dataset

log = get_logger("yahoo_fundamentals")
URL = "https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{t}"
FIELDS = ["TotalRevenue", "OperatingRevenue", "EBITDA", "NormalizedEBITDA", "EBIT", "OperatingIncome",
          "NetIncomeCommonStockholders", "NetIncome", "DilutedEPS", "OperatingCashFlow", "CapitalExpenditure",
          "FreeCashFlow", "TotalDebt", "CashAndCashEquivalents", "CashCashEquivalentsAndShortTermInvestments",
          "StockholdersEquity", "InvestedCapital", "TotalAssets", "CurrentLiabilities", "InterestExpense",
          "OrdinarySharesNumber", "TaxRateForCalcs"]


def fetch_symbol(sym: str, sess: requests.Session) -> list[dict]:
    types = ",".join([f"annual{f}" for f in FIELDS] + [f"quarterly{f}" for f in FIELDS])
    params = {"symbol": f"{sym}.NS", "type": types, "period1": 1420070400, "period2": int(time.time())}
    for attempt in range(3):
        try:
            r = sess.get(URL.format(t=f"{sym}.NS"), params=params, timeout=30)
            if r.status_code == 200:
                rows = []
                for res in r.json().get("timeseries", {}).get("result", []) or []:
                    t = res.get("meta", {}).get("type", [None])[0]
                    if not t:
                        continue
                    freq = "annual" if t.startswith("annual") else "quarterly"
                    field = t[len(freq):]
                    for x in res.get(t, []) or []:
                        if x and x.get("reportedValue"):
                            rows.append({"symbol": sym, "freq": freq, "field": field,
                                         "period_end": x["asOfDate"], "value": x["reportedValue"]["raw"],
                                         "currency": x.get("currencyCode")})
                return rows
            if r.status_code in (404,):
                return []
        except (requests.RequestException, json.JSONDecodeError) as exc:
            log.warning("%s attempt %d: %s", sym, attempt + 1, exc)
        time.sleep(2 + 2 * attempt)
    return []


def run(symbols: list[str], workers: int = 2) -> pd.DataFrame:
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0 (research)"})
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {}
        for s in symbols:
            futs[ex.submit(fetch_symbol, s, sess)] = s
            time.sleep(0.4)
        for i, f in enumerate(as_completed(futs), 1):
            out.extend(f.result())
            if i % 200 == 0:
                log.info("yahoo progress %d/%d", i, len(symbols))
    df = pd.DataFrame(out)
    if not df.empty:
        df["period_end"] = pd.to_datetime(df.period_end)
        ensure_dir(path("processed"))
        df.to_parquet(path("processed") / "fundamentals_yahoo.parquet", index=False)
        register_dataset(
            "fundamentals_yahoo", source_url="https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/",
            coverage_start=str(df.period_end.min().date()), coverage_end=str(df.period_end.max().date()),
            n_records=int(len(df)), n_symbols=int(df.symbol.nunique()), quality_score=0.6,
            limitations=("SUPPLEMENTARY. ~4 annual periods (FY2023-FY2026) + ~5 quarters; latest restated values "
                         "(not as-first-reported); current listings only (survivorship). Availability dates are "
                         "imputed from NSE board-meeting dates or statutory deadlines."),
            path="data/processed/fundamentals_yahoo.parquet")
    log.info("yahoo fundamentals: %d rows for %d symbols", len(df), df.symbol.nunique() if len(df) else 0)
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols-file", default=None)
    a = ap.parse_args()
    if a.symbols_file:
        syms = [s.strip() for s in open(a.symbols_file) if s.strip()]
    else:
        from src.data_acquisition.shp_xbrl import default_symbols
        syms = sorted(default_symbols())
    run(syms)


if __name__ == "__main__":
    main()
