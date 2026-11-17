"""Quarterly shareholding-pattern (SHP) XBRL filings: download + parse.

Discovery: the per-symbol filing lists (URL, quarter, submission and broadcast timestamps) come
from the CC0 dataset github.com/aditya-jha/nse-historical-membership
(`shareholding_history/data/filings_index/*.json`), which was built from NSE's
`corporate-share-holdings-master` endpoint. We cannot query that endpoint ourselves
(www.nseindia.com refuses this environment), so we re-use the published index and download
each XBRL instance from the public archive host `nsearchives.nseindia.com/corporate/xbrl/`.

Parsing (our own, not the repo's): every category-level fact (single explicit dimension member)
and every named >1% holder (typed "DetailsOfSharesHeldBy...Axis" contexts) is extracted, so that
mutual funds, insurers, FPIs (cat I/II), FDI, AIFs, banks, pension funds etc. can be separated.
The repo's own `_flat.csv` only carries FII/DII for 2022+; we rebuild them for 2018+.

Outputs (data/processed/):
  shp_filings.parquet      one row per filing (symbol, period_end, dates, url, scale, total shares)
  shp_categories.parquet   long: filing x category member -> pct, shares
  shp_named_holders.parquet long: filing x named holder -> category axis, name, shares, pct
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import gzip
import json
import os
import re
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

from src.common import SETTINGS, ensure_dir, get_logger, path, register_dataset

log = get_logger("shp_xbrl")
ACQ = SETTINGS["acquisition"]
EXT = path("raw") / "external" / "nse-historical-membership"
FILINGS_INDEX = EXT / "shareholding_history" / "data" / "filings_index"
RAW_DIR = path("raw") / "shp_xbrl"

CTX_RE = re.compile(r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>', re.DOTALL)
DIM_RE = re.compile(r'<xbrldi:explicitMember dimension="[^"]+">([^<]+)</xbrldi:explicitMember>')
TYP_RE = re.compile(r'<xbrldi:typedMember dimension="([^"]+)">')
PERIOD_RE = re.compile(r'<xbrli:(?:instant|endDate)>([0-9-]+)</xbrli:(?:instant|endDate)>')
FACT_RE = re.compile(
    r'<in-bse-shp:(ShareholdingAsAPercentageOfTotalNumberOfShares|NumberOfShares|NameOfTheShareholder)'
    r' contextRef="([^"]+)"[^>]*>([^<]*)</in-bse-shp:')
TAX_RE = re.compile(r'xmlns:in-bse-shp="(http://[^"]+)"')
SYM_RE = re.compile(r'<xbrli:identifier scheme="[^"]*">([^<]+)</xbrli:identifier>')


# --------------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------------
def _parse_date(s: str | None, fmt: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(s.strip(), fmt)
    except ValueError:
        return None


def load_filings_index(symbols: set[str] | None = None, min_period: str = "2017-09") -> pd.DataFrame:
    rows = []
    for fp in sorted(FILINGS_INDEX.glob("*.json")):
        sym = fp.stem
        if symbols is not None and sym not in symbols:
            continue
        d = json.loads(fp.read_text())
        for f in d.get("filings", []):
            url = f.get("xbrl") or ""
            if not url.endswith(".xml"):
                continue
            pe = _parse_date(f.get("date"), "%d-%b-%Y")
            if pe is None or pe.strftime("%Y-%m") < min_period:
                continue
            rows.append({
                "symbol": sym,
                "period_end": pe.date(),
                "submission_date": _parse_date(f.get("submission_date"), "%d-%b-%Y"),
                "broadcast_ts": _parse_date(f.get("broadcast_date"), "%d-%b-%Y %H:%M:%S"),
                "revised": (f.get("revised") or "N") != "N",
                "record_id": f.get("record_id"),
                "url": url,
                "filename": url.rsplit("/", 1)[-1],
            })
    df = pd.DataFrame(rows)
    if not df.empty:
        # filename embeds the dissemination timestamp DDMMYYYYhhmmss (12-hour clock, no AM/PM)
        ts = df["filename"].str.extract(r"_(\d{14})_WEB\.xml$")[0]
        df["filename_date"] = pd.to_datetime(ts.str[:8], format="%d%m%Y", errors="coerce")
    return df


# --------------------------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------------------------
_local = threading.local()


def _sess() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": ACQ["user_agent"]})
        _local.s = s
    return s


def raw_path(symbol: str, filename: str) -> Path:
    return RAW_DIR / symbol / (filename + ".gz")


def download_one(symbol: str, url: str, filename: str) -> str:
    dest = raw_path(symbol, filename)
    miss = Path(str(dest) + ".404")
    if dest.exists() or miss.exists():
        return "skip"
    for attempt in range(ACQ["retries"]):
        try:
            time.sleep(ACQ["min_interval_s"])
            r = _sess().get(url, timeout=ACQ["timeout_s"])
            if r.status_code == 404:
                ensure_dir(dest.parent)
                miss.write_text(url)
                return "404"
            if r.status_code == 200 and r.content.lstrip().startswith(b"<"):
                ensure_dir(dest.parent)
                tmp = dest.with_suffix(".part")
                with gzip.open(tmp, "wb", compresslevel=6) as fh:
                    fh.write(r.content)
                tmp.replace(dest)
                return "ok"
            log.warning("HTTP %s / non-xml for %s", r.status_code, url)
        except requests.RequestException as exc:
            log.warning("attempt %d %s: %s", attempt + 1, url, exc)
        time.sleep(2 ** attempt)
    return "fail"


def download(filings: pd.DataFrame, workers: int = 3) -> dict:
    stats: dict[str, int] = {}
    jobs = filings[["symbol", "url", "filename"]].drop_duplicates().itertuples(index=False)
    jobs = list(jobs)
    log.info("downloading %d SHP XBRL files", len(jobs))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(download_one, *j) for j in jobs]
        for i, f in enumerate(as_completed(futs), 1):
            stats[f.result()] = stats.get(f.result(), 0) + 1
            if i % 1000 == 0:
                log.info("progress %d/%d %s", i, len(jobs), stats)
    log.info("download done %s", stats)
    return stats


# --------------------------------------------------------------------------------------------
# Parse
# --------------------------------------------------------------------------------------------
def _num(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _holder_key(cid: str) -> str:
    """Pair a named holder's name context with its numbers context.
    2018-2025 taxonomies: '<Axis>001D' (name) / '<Axis>001I' (numbers).
    2025-10 V1.1 taxonomy: 'D_<Axis>_Context16' (name) / '<Axis>_Context16' (numbers)."""
    if cid.startswith(("D_", "I_")):
        return cid[2:]
    if len(cid) > 1 and cid[-1] in "DI" and cid[-2].isdigit():
        return cid[:-1]
    return cid


def parse_xbrl_text(txt: str) -> dict:
    ctx_members: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    period_end = None
    for cid, body in CTX_RE.findall(txt):
        mem = tuple(m.split(":")[-1] for m in DIM_RE.findall(body))
        typ = tuple(t.split(":")[-1] for t in TYP_RE.findall(body))
        ctx_members[cid] = (mem, typ)
        if period_end is None:
            pm = PERIOD_RE.search(body)
            if pm:
                period_end = pm.group(1)
    cat_pct: dict[str, float] = {}
    cat_sh: dict[str, float] = {}
    names: dict[str, str] = {}
    nh_pct: dict[str, float] = {}
    nh_sh: dict[str, float] = {}
    for tag, cid, val in FACT_RE.findall(txt):
        mem, typ = ctx_members.get(cid, ((), ()))
        if typ:  # named-holder row: name and numbers sit in paired contexts
            key = _holder_key(cid)
            if tag == "NameOfTheShareholder":
                names[key] = (typ[0], val.strip())
            elif tag == "NumberOfShares":
                nh_sh[key] = _num(val)
            else:
                nh_pct[key] = _num(val)
            continue
        if len(mem) != 1:
            continue
        m = mem[0]
        if tag == "ShareholdingAsAPercentageOfTotalNumberOfShares":
            cat_pct.setdefault(m, _num(val))
        elif tag == "NumberOfShares":
            cat_sh.setdefault(m, _num(val))
    # scale: total pattern should be 100 (percent) or 1 (fraction) or 10000 (bps*100)
    tot = cat_pct.get("ShareholdingPatternMember")
    if tot is None:
        pr, pu = cat_pct.get("ShareholdingOfPromoterAndPromoterGroupMember"), cat_pct.get("PublicShareholdingMember")
        tot = (pr or 0) + (pu or 0) if (pr is not None or pu is not None) else None
    scale = 1.0
    if tot:
        if tot > 1000:
            scale = 0.01
        elif tot < 2:
            scale = 100.0
    cats = [{"member": m, "pct": (v * scale if v is not None else None), "shares": cat_sh.get(m)}
            for m, v in cat_pct.items()]
    for m, s in cat_sh.items():
        if m not in cat_pct:
            cats.append({"member": m, "pct": None, "shares": s})
    holders = []
    for key, (axis, name) in names.items():
        p = nh_pct.get(key)
        holders.append({"axis": axis, "name": name, "shares": nh_sh.get(key),
                        "pct": p * scale if p is not None else None})
    tm = TAX_RE.search(txt[:5000])
    sm = SYM_RE.search(txt)
    return {"period_end_xbrl": period_end, "scale": scale, "taxonomy": tm.group(1) if tm else None,
            "xbrl_symbol": sm.group(1) if sm else None, "categories": cats, "holders": holders}


def _parse_file(args: tuple[str, str]) -> tuple[str, dict | None]:
    symbol, filename = args
    p = raw_path(symbol, filename)
    try:
        with gzip.open(p, "rt", encoding="utf-8", errors="replace") as fh:
            return filename, parse_xbrl_text(fh.read())
    except Exception as exc:  # noqa: BLE001 - corrupt file: record and continue
        log.warning("parse failed %s: %s", p, exc)
        return filename, None


def parse_all(filings: pd.DataFrame, workers: int = 4) -> None:
    filings = filings[[raw_path(s, f).exists() for s, f in zip(filings.symbol, filings.filename)]]
    jobs = list(filings[["symbol", "filename"]].drop_duplicates().itertuples(index=False, name=None))
    log.info("parsing %d files", len(jobs))
    meta, cats, holders = {}, [], []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for fn, res in ex.map(_parse_file, jobs, chunksize=64):
            if res is None:
                continue
            meta[fn] = {k: res[k] for k in ("period_end_xbrl", "scale", "taxonomy", "xbrl_symbol")}
            tot = next((c["shares"] for c in res["categories"] if c["member"] == "ShareholdingPatternMember"), None)
            meta[fn]["total_shares"] = tot
            for c in res["categories"]:
                cats.append({"filename": fn, **c})
            for h in res["holders"]:
                holders.append({"filename": fn, **h})
    fil = filings.copy()
    m = pd.DataFrame.from_dict(meta, orient="index").rename_axis("filename").reset_index()
    fil = fil.merge(m, on="filename", how="left")
    out = ensure_dir(path("processed"))
    fil.to_parquet(out / "shp_filings.parquet", index=False)
    pd.DataFrame(cats).to_parquet(out / "shp_categories.parquet", index=False)
    pd.DataFrame(holders).to_parquet(out / "shp_named_holders.parquet", index=False)
    register_dataset(
        "shp_xbrl",
        source_url="https://nsearchives.nseindia.com/corporate/xbrl/ (URLs from aditya-jha/nse-historical-membership filings_index)",
        coverage_start=str(fil.period_end.min()), coverage_end=str(fil.period_end.max()),
        n_records=int(len(fil)), n_symbols=int(fil.symbol.nunique()),
        missingness=f"{fil.total_shares.isna().mean():.3f} of filings without total shares",
        quality_score=0.85,
        limitations=("Filings index only covers symbols listed in 2026 (delisted/merged companies such as "
                     "HDFC, DHFL are absent: survivorship gap). XBRL exists from ~Dec-2017 only. Named holders "
                     "are only those >1% (SEBI disclosure threshold)."),
        path="data/processed/shp_*.parquet")
    log.info("parsed: %d filings, %d category facts, %d named holders", len(fil), len(cats), len(holders))


def default_symbols(extra: list[str] | None = None) -> set[str]:
    """PIT index constituents (broad indices) since mid-2017 + extra symbols."""
    mem = pd.read_csv(EXT / "index_history" / "data" / "index_membership_history.csv")
    broad = ["Nifty 50", "Nifty 500", "Nifty Midcap 150", "Nifty Smallcap 250", "Nifty Microcap 250"]
    mem = mem[mem.index_name.isin(broad) & (mem.valid_to.isna() | (mem.valid_to >= "2017-06-01"))]
    syms = set(mem.symbol)
    extra_file = path("reference") / "shp_extra_symbols.txt"
    if extra_file.exists():
        syms |= {s.strip() for s in extra_file.read_text().split() if s.strip()}
    syms |= set(extra or []) | {"SAILIFE", "ETERNAL", "MEESHO"}
    return syms


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", default="download,parse")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--all-symbols", action="store_true", help="every symbol in the filings index")
    a = ap.parse_args()
    syms = None if a.all_symbols else default_symbols()
    filings = load_filings_index(syms, SETTINGS["acquisition"]["shp_first_period"])
    log.info("%d filings for %d symbols", len(filings), filings.symbol.nunique())
    if "download" in a.step:
        download(filings, a.workers)
    if "parse" in a.step:
        parse_all(filings, workers=os.cpu_count() or 4)


if __name__ == "__main__":
    main()
