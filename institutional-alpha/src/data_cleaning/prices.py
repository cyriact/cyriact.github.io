"""Build the daily equity price panel from NSE bhavcopies + delivery (MTO) files.

Corporate-action adjustment
---------------------------
NSE's bhavcopy PREVCLOSE is simply the previous session's close: it is NOT rebased for splits,
bonuses or dividends (verified: Yes Bank's 2017 split and 500/500 large ex-dividend dates show
PREVCLOSE == prior close). We therefore build adjustment factors ourselves from NSE's
corporate-action files (PR bundle `Bc*.csv`):
    split  "FV SPLT FRM RS 10 TO RS 2"   -> share factor 10/2 = 5
    bonus  "BONUS 1:2" (1 new per 2 held) -> share factor (1+2)/2 = 1.5
    rights issues are parsed for audit but NOT adjusted (subscription price incl. face value is not
    reliably parsable); this slightly understates returns on rights ex-dates (documented).
and validate each factor against the actual ex-date price jump (a factor whose implied jump is
off by >40% is shifted to the best-matching session within +-3 days or dropped). Large jumps
(>45%) that exactly match a common split ratio but have no recorded action are adjusted and
flagged `ca_inferred`. Then, with F_t the share factor effective on ex-date t:
    price_return_t = CLOSE_t * F_t / CLOSE_{t-1} - 1
    total_return_t = (CLOSE_t + DPS_t) * F_t / CLOSE_{t-1} - 1     (cash dividends on ex-date)
Adjusted close / total-return index are rebuilt by compounding these returns.

Symbols are chained to their latest symbol using NSE's symbolchange.csv (e.g. ZOMATO -> ETERNAL).
"""
from __future__ import annotations

import glob
import io
import os
import re
import zipfile
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from src.common import SETTINGS, ensure_dir, get_logger, path, register_dataset

log = get_logger("prices")
RAW = path("raw")
KEEP_SERIES = set(SETTINGS["universe"]["series"])


# ----------------------------------------------------------------------------- parsers
def _date_from_filename(fp: str) -> pd.Timestamp:
    """Session date from the archive file name (the TIMESTAMP column mixes 2- and 4-digit years)."""
    name = os.path.basename(fp)
    m = re.search(r"cm(\d{2}[A-Z]{3}\d{4})bhav", name)
    if m:
        return pd.to_datetime(m.group(1), format="%d%b%Y")
    m = re.search(r"_(\d{8})_F_", name)
    return pd.to_datetime(m.group(1), format="%Y%m%d")


def _read_bhav(fp: str) -> pd.DataFrame | None:
    try:
        with zipfile.ZipFile(fp) as z:
            raw = z.read(z.namelist()[0])
    except zipfile.BadZipFile:
        return None
    df = pd.read_csv(io.BytesIO(raw))
    df.columns = [c.strip() for c in df.columns]
    if "TckrSymb" in df.columns:  # UDiFF format (2024-07-08 onwards)
        df = df[df["FinInstrmTp"].astype(str).str.strip() == "STK"]
        out = pd.DataFrame({
            "date": pd.to_datetime(df["TradDt"]),
            "symbol": df["TckrSymb"].astype(str).str.strip(),
            "series": df["SctySrs"].astype(str).str.strip(),
            "open": df["OpnPric"], "high": df["HghPric"], "low": df["LwPric"],
            "close": df["ClsPric"], "last": df["LastPric"], "prevclose": df["PrvsClsgPric"],
            "volume": df["TtlTradgVol"], "value": df["TtlTrfVal"],
            "trades": df["TtlNbOfTxsExctd"], "isin": df["ISIN"].astype(str).str.strip(),
        })
    else:
        out = pd.DataFrame({
            "date": _date_from_filename(fp),
            "symbol": df["SYMBOL"].astype(str).str.strip(),
            "series": df["SERIES"].astype(str).str.strip(),
            "open": df["OPEN"], "high": df["HIGH"], "low": df["LOW"], "close": df["CLOSE"],
            "last": df["LAST"], "prevclose": df["PREVCLOSE"], "volume": df["TOTTRDQTY"],
            "value": df["TOTTRDVAL"], "trades": df.get("TOTALTRADES", np.nan),
            "isin": df.get("ISIN", pd.Series("", index=df.index)).astype(str).str.strip(),
        })
    return out[out.series.isin(KEEP_SERIES)]


def _read_mto(fp: str) -> pd.DataFrame | None:
    m = re.search(r"MTO_(\d{8})\.DAT$", fp)
    date = pd.to_datetime(m.group(1), format="%d%m%Y")
    rows = []
    with open(fp, "r", encoding="latin-1") as fh:
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) >= 7 and parts[0] == "20":
                rows.append((parts[2].strip(), parts[3].strip(), parts[4], parts[5], parts[6]))
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["symbol", "series", "mto_qty", "deliv_qty", "deliv_pct"])
    df = df[df.series.isin(KEEP_SERIES)]
    for c in ("mto_qty", "deliv_qty", "deliv_pct"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = date
    return df


def _parallel(fn, files: list[str]) -> pd.DataFrame:
    with ProcessPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        parts = [p for p in ex.map(fn, files, chunksize=32) if p is not None and len(p)]
    return pd.concat(parts, ignore_index=True)


# ----------------------------------------------------------------------------- symbol chain
def load_symbol_changes() -> pd.DataFrame:
    sc = pd.read_csv(RAW / "reference" / "symbolchange.csv", encoding="latin-1")
    sc.columns = ["company", "old", "new", "eff"]
    sc["old"] = sc.old.str.strip()
    sc["new"] = sc.new.str.strip()
    sc["eff"] = pd.to_datetime(sc.eff.str.strip(), format="%d-%b-%Y", errors="coerce")
    return sc.dropna(subset=["eff"]).sort_values("eff")


def canonicalize(df: pd.DataFrame, sc: pd.DataFrame) -> pd.DataFrame:
    """Map historical symbols to their latest symbol. A rename applies to rows dated before the
    effective date (the old ticker may later be re-used by another company)."""
    df = df.copy()
    df["symbol_raw"] = df["symbol"]
    sym = df["symbol"].to_numpy(dtype=object).copy()
    dates = df["date"].to_numpy()
    for _ in range(4):  # follow chains A->B->C
        changed = 0
        olds = set(sc.old)
        cand = np.flatnonzero(pd.Series(sym).isin(olds).to_numpy())
        if not len(cand):
            break
        sub = pd.DataFrame({"i": cand, "symbol": sym[cand], "date": dates[cand]})
        for old, grp in sub.groupby("symbol"):
            gd = grp["date"].to_numpy()
            gi = grp["i"].to_numpy()
            todo = np.ones(len(gi), dtype=bool)
            # changes sorted by effective date: a row maps to the earliest change after its date
            for new, eff in sc.loc[sc.old == old, ["new", "eff"]].itertuples(index=False):
                m = todo & (gd < np.datetime64(eff))
                if m.any():
                    sym[gi[m]] = new
                    changed += int(m.sum())
                    todo &= ~m
        if not changed:
            break
    df["symbol"] = sym
    return df


# ----------------------------------------------------------------------------- corporate actions
DIV_RE = re.compile(r"(?:DIV(?:IDEND)?)[^0-9]{0,25}?(?:RS|RE|INR)\.?\s*([0-9]+(?:\.[0-9]+)?)", re.I)


def parse_dividend(purpose: str) -> float:
    """Sum of cash dividends per share in an NSE corporate-action PURPOSE string."""
    if not isinstance(purpose, str):
        return 0.0
    p = purpose.upper().replace("PERSH", " PER SH")
    if "DIV" not in p:
        return 0.0
    vals = [float(x) for x in DIV_RE.findall(p)]
    return float(sum(vals))


SPLIT_RE = re.compile(r"(?:SPLT|SPLIT|SUB-?DIV\w*|CONSOLIDAT\w*|CNSLDAT)\D{0,40}?(?:RS|RE|INR)?\.?\s*(\d+(?:\.\d+)?)"
                      r"\D{0,30}?TO\s*(?:RS|RE|INR)?\.?\s*(\d+(?:\.\d+)?)", re.I)
BONUS_RE = re.compile(r"(?:BONUS|BNS)\D{0,15}?(\d+)\s*:\s*(\d+)", re.I)
RIGHTS_RE = re.compile(r"(?:RIGHTS|RGHTS)\D{0,10}?(\d+)\s*:\s*(\d+)\D{0,20}?@?\s*(?:PREM(?:IUM)?)?\D{0,10}?(?:RS|RE)\.?\s*(\d+(?:\.\d+)?)", re.I)


def parse_share_factor(purpose: str) -> float:
    """Shares-held multiplier implied by splits / consolidations / bonus issues (1.0 if none)."""
    if not isinstance(purpose, str):
        return 1.0
    p = purpose.upper()
    f = 1.0
    m = SPLIT_RE.search(p)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0 and b > 0 and a != b:
            f *= a / b
    m = BONUS_RE.search(p)
    if m:
        x, y = float(m.group(1)), float(m.group(2))
        if x > 0 and y > 0:
            f *= (x + y) / y
    return f


def parse_rights(purpose: str) -> tuple[float, float, float] | None:
    """(new, held, premium) from a rights PURPOSE string; face value is unknown so the premium is
    used as a lower bound of the subscription price (conservative: understates the adjustment)."""
    if not isinstance(purpose, str):
        return None
    m = RIGHTS_RE.search(purpose.upper())
    if not m:
        return None
    return float(m.group(1)), float(m.group(2)), float(m.group(3))


def build_corporate_actions() -> pd.DataFrame:
    files = sorted(glob.glob(str(RAW / "pr" / "*" / "*" / "[Bb][Cc]*.csv")))
    parts = []
    for fp in files:
        try:
            d = pd.read_csv(fp, dtype=str, encoding="latin-1")
        except Exception:  # noqa: BLE001
            continue
        d.columns = [c.strip().upper() for c in d.columns]
        if "SYMBOL" not in d.columns or "EX_DT" not in d.columns:
            continue
        d["file_date"] = pd.to_datetime(os.path.basename(os.path.dirname(fp)), format="%Y%m%d")
        parts.append(d[["SERIES", "SYMBOL", "EX_DT", "PURPOSE", "RECORD_DT", "file_date"]])
    ca = pd.concat(parts, ignore_index=True)
    for c in ("SERIES", "SYMBOL", "PURPOSE"):
        ca[c] = ca[c].astype(str).str.strip()
    ex = ca.EX_DT.astype(str).str.strip()
    ca["ex_date"] = pd.to_datetime(ex, format="%d/%m/%Y", errors="coerce")
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):  # NSE switched formats over time
        miss = ca["ex_date"].isna()
        ca.loc[miss, "ex_date"] = pd.to_datetime(ex[miss], format=fmt, errors="coerce")
    # some records carry only a record date: use it (ex-date = record date under T+1; the price-jump
    # validation in _share_factors searches +-3 sessions)
    rd = ca.RECORD_DT.astype(str).str.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y"):
        miss = ca["ex_date"].isna()
        ca.loc[miss, "ex_date"] = pd.to_datetime(rd[miss], format=fmt, errors="coerce")
    ca = ca.dropna(subset=["ex_date"])
    # first announcement date of each (symbol, ex_date, purpose)
    ca = (ca.sort_values("file_date")
            .drop_duplicates(["SYMBOL", "ex_date", "PURPOSE"], keep="first")
            .rename(columns={"SYMBOL": "symbol", "SERIES": "series", "PURPOSE": "purpose",
                             "file_date": "first_seen"}))
    ca["dps"] = ca.purpose.map(parse_dividend)
    ca["share_factor"] = ca.purpose.map(parse_share_factor)
    ca["rights"] = ca.purpose.map(parse_rights)
    up = ca.purpose.str.upper()
    ca["is_split"] = up.str.contains("SPLT|SPLIT|SUB-DIV|SUBDIV")
    ca["is_bonus"] = up.str.contains("BONUS")
    ca["is_rights"] = up.str.contains("RIGHTS|RGHTS")
    return ca[["symbol", "series", "ex_date", "purpose", "dps", "share_factor", "rights", "is_split", "is_bonus",
               "is_rights", "first_seen", "RECORD_DT"]].rename(columns={"RECORD_DT": "record_date"})


def build_board_meetings() -> pd.DataFrame:
    files = sorted(glob.glob(str(RAW / "pr" / "*" / "*" / "[Bb][Mm]*.txt")))
    rows = []
    rx = re.compile(r"^(.*?)\s+(\S+)\s*:\s*(\d{2}-[A-Za-z]{3}-\d{4})\s*:\s*(.*)$")
    for fp in files:
        fd = pd.to_datetime(os.path.basename(os.path.dirname(fp)), format="%Y%m%d")
        with open(fp, "r", encoding="latin-1") as fh:
            for line in fh:
                m = rx.match(line.strip())
                if m:
                    rows.append((m.group(2).strip(), m.group(3), m.group(4).strip(), fd))
    # 2022: NSE's Bm files are mostly empty; recover results dates from "Outcome of Board Meeting"
    # announcements (dissemination date = An file date). Partial coverage, documented.
    rx_an = re.compile(r"^(.*?)\s+(\S+)\s*:\s*Outcome of Board Meeting(.*)$", re.I)
    for fp in sorted(glob.glob(str(RAW / "pr_an" / "*" / "*" / "[Aa][Nn]*.txt"))):
        fd = pd.to_datetime(os.path.basename(os.path.dirname(fp)), format="%Y%m%d")
        with open(fp, "r", encoding="latin-1") as fh:
            for line in fh:
                m = rx_an.match(line.strip())
                if m and re.search(r"result", m.group(3), re.I):
                    rows.append((m.group(2).strip(), fd.strftime("%d-%b-%Y").upper(),
                                 "Financial Results (outcome announcement)", fd))
    bm = pd.DataFrame(rows, columns=["symbol", "bm_date", "purpose", "first_seen"])
    bm["bm_date"] = pd.to_datetime(bm.bm_date, format="%d-%b-%Y", errors="coerce")
    bm = bm.dropna(subset=["bm_date"]).sort_values("first_seen")
    bm = bm.drop_duplicates(["symbol", "bm_date"], keep="first")
    bm["is_results"] = bm.purpose.str.contains("Financial Result|Results", case=False)
    return bm


# ----------------------------------------------------------------------------- main build
def build(write: bool = True) -> pd.DataFrame:
    bfiles = sorted(glob.glob(str(RAW / "bhavcopy" / "*" / "*.zip")))
    log.info("parsing %d bhavcopies", len(bfiles))
    px = _parallel(_read_bhav, bfiles)
    for c in ("open", "high", "low", "close", "last", "prevclose", "volume", "value", "trades"):
        px[c] = pd.to_numeric(px[c], errors="coerce")
    mfiles = sorted(glob.glob(str(RAW / "mto" / "*" / "MTO_*.DAT")))
    log.info("parsing %d MTO files", len(mfiles))
    mto = _parallel(_read_mto, mfiles)
    px = px.merge(mto, on=["date", "symbol", "series"], how="left")

    # one row per symbol/date: prefer EQ over BE/BZ
    px["series_rank"] = px.series.map({"EQ": 0, "BE": 1, "BZ": 2}).fillna(3)
    px = px.sort_values(["symbol", "date", "series_rank"]).drop_duplicates(["symbol", "date"])
    px = px[px["isin"].str.startswith("INE") | (px["isin"] == "") | px["isin"].isna() | (px["isin"] == "nan")]

    sc = load_symbol_changes()
    px = canonicalize(px, sc)
    px = px.sort_values(["symbol", "date", "series_rank"]).drop_duplicates(["symbol", "date"])
    px = px.drop(columns="series_rank").sort_values(["symbol", "date"]).reset_index(drop=True)

    g = px.groupby("symbol", sort=False)
    px["prior_close"] = g["close"].shift(1)
    first = px["prior_close"].isna()
    raw_ratio = px["close"] / px["prior_close"]

    ca = build_corporate_actions()
    ca_c = canonicalize(ca.rename(columns={"ex_date": "date"}), sc).rename(columns={"date": "ex_date"})
    px["factor"], audit, dem = _share_factors(px, ca_c, raw_ratio)
    px["demerger_adj"] = dem
    # shares multiplier on ex-date (splits/bonus/consolidation only; demerger neutralisation excluded)
    px["ca_ratio"] = px["factor"].where(~dem, 1.0)
    px["ca_flag"] = (px["ca_ratio"] - 1).abs() > 1e-9
    px["ca_inferred"] = audit

    divs = ca_c.groupby(["symbol", "ex_date"], as_index=False)["dps"].sum()
    divs = divs[divs.dps > 0]
    px = px.merge(divs.rename(columns={"ex_date": "date"}), on=["symbol", "date"], how="left")
    px["dps"] = px["dps"].fillna(0.0)
    # dividends quoted per pre-action share: ignore implausible yields (parse errors)
    dy = px["dps"] / (px["prior_close"] / px["factor"])
    px.loc[(dy > 0.25) | dy.isna(), "dps"] = 0.0
    px["ret_px"] = px["close"] * px["factor"] / px["prior_close"] - 1.0
    px["ret_tr"] = (px["close"] + px["dps"]) * px["factor"] / px["prior_close"] - 1.0
    px.loc[first, ["ret_px", "ret_tr"]] = np.nan  # listing day: no holdable return

    # adjusted close (price-only and total-return), anchored at the latest close
    px["adj_close"] = _anchor(px, "ret_px")
    px["tr_index"] = _anchor(px, "ret_tr")
    px["value_cr"] = px["value"] / 1e7

    if write:
        out = ensure_dir(path("processed"))
        px.to_parquet(out / "prices_daily.parquet", index=False)
        ca_c.to_parquet(out / "corporate_actions.parquet", index=False)
        bm = canonicalize(build_board_meetings().rename(columns={"bm_date": "date"}), sc).rename(
            columns={"date": "bm_date"})
        bm.to_parquet(out / "board_meetings.parquet", index=False)
        register_dataset(
            "prices_daily",
            source_url="https://nsearchives.nseindia.com/content/historical/EQUITIES/ ; /content/cm/ ; /archives/equities/mto/",
            coverage_start=str(px.date.min().date()), coverage_end=str(px.date.max().date()),
            n_records=int(len(px)), n_symbols=int(px.symbol.nunique()),
            missingness=f"delivery missing {px.deliv_qty.isna().mean():.3f}",
            quality_score=0.95,
            limitations="Mainboard EQ/BE/BZ only. Dividends credited from parsed corporate-action text; "
                        "rare unparsed purposes are missed. Delisted names keep their history until last trade.",
            path="data/processed/prices_daily.parquet")
        register_dataset(
            "corporate_actions", source_url="https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR*.zip (Bc files)",
            coverage_start=str(ca_c.ex_date.min().date()), coverage_end=str(ca_c.ex_date.max().date()),
            n_records=int(len(ca_c)), quality_score=0.85,
            limitations="Dividend amounts and split/bonus/consolidation factors parsed from free-text PURPOSE; "
                        "factors validated against ex-date price jumps; rights issues not adjusted; demergers neutralised.",
            path="data/processed/corporate_actions.parquet")
        register_dataset(
            "board_meetings", source_url="PR*.zip (Bm files)", n_records=int(len(bm)), quality_score=0.8,
            coverage_start=str(bm.bm_date.min().date()), coverage_end=str(bm.bm_date.max().date()),
            limitations="Scheduled board-meeting dates (results announcements) as disseminated by NSE.",
            path="data/processed/board_meetings.parquet")
        log.info("prices: %d rows, %d symbols, %s..%s", len(px), px.symbol.nunique(), px.date.min(), px.date.max())
    return px


NICE_RATIOS = np.array([1.25, 4 / 3, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 10.0, 20.0])


def _share_factors(px: pd.DataFrame, ca: pd.DataFrame, raw_ratio: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Per-row share factor on ex-dates, validated against the observed price jump."""
    factor = pd.Series(1.0, index=px.index)
    inferred = pd.Series(False, index=px.index)
    demerger = pd.Series(False, index=px.index)
    key = pd.Series(px.index, index=pd.MultiIndex.from_arrays([px.symbol, px.date]))
    ev = ca[(ca.share_factor != 1.0)].groupby(["symbol", "ex_date"]).share_factor.prod()
    sym_rows = {s: idx for s, idx in px.groupby("symbol", sort=False).indices.items()}
    dates = px["date"].to_numpy()
    rr = raw_ratio.to_numpy()
    used = set()
    n_ok = n_shift = n_drop = 0
    for (s, d), f in ev.items():
        rows = sym_rows.get(s)
        if rows is None:
            continue
        pos = np.searchsorted(dates[rows], np.datetime64(d))
        best, best_err = None, np.inf
        for off in (0, 1, -1, 2, -2, 3, -3):
            k = pos + off
            if 0 < k < len(rows):
                i = rows[k]
                if np.isfinite(rr[i]) and rr[i] > 0:
                    err = abs(np.log(rr[i] * f))
                    if err < best_err:
                        best, best_err = i, err
        if best is not None and best_err < np.log(1.4) and best not in used:
            factor.iloc[best] *= f
            used.add(best)
            n_ok += int(best == rows[min(pos, len(rows) - 1)])
            n_shift += int(best != rows[min(pos, len(rows) - 1)])
        else:
            n_drop += 1
    # demergers / schemes of arrangement: the ex-date drop is value distributed to holders (shares
    # of the resulting company list later). Neutralise the ex-date return (factor = prior/close).
    dem = ca[ca.purpose.str.upper().str.contains("DEMERGER|ARRANGEMENT|SPIN", na=False)][["symbol", "ex_date"]].drop_duplicates()
    n_dem = 0
    for s, d in dem.itertuples(index=False):
        rows = sym_rows.get(s)
        if rows is None:
            continue
        pos = np.searchsorted(dates[rows], np.datetime64(d))
        for off in (0, 1):
            k = pos + off
            if 0 < k < len(rows):
                i = rows[k]
                if np.isfinite(rr[i]) and 0 < rr[i] < 0.9 and factor.iloc[i] == 1.0:
                    factor.iloc[i] = 1.0 / rr[i]
                    inferred.iloc[i] = True
                    demerger.iloc[i] = True
                    n_dem += 1
                    break
    # unexplained jumps matching a common split ratio (within 2%, or 8% for drops of >= 2/3)
    big = np.isfinite(rr) & (rr > 0) & (np.abs(np.log(np.where(rr > 0, rr, 1))) > np.log(1.8)) & (factor.to_numpy() == 1.0)
    for i in np.flatnonzero(big):
        if rr[i] < 1:  # split / bonus not on record
            inv = 1.0 / rr[i]
            j = np.argmin(np.abs(np.log(NICE_RATIOS) - np.log(inv)))
            tol = 0.08 if NICE_RATIOS[j] >= 3 else 0.02
            if abs(np.log(NICE_RATIOS[j]) - np.log(inv)) < tol:
                factor.iloc[i] = NICE_RATIOS[j]
                inferred.iloc[i] = True
        else:  # consolidation not on record (tighter tolerance: genuine rallies exist)
            j = np.argmin(np.abs(np.log(NICE_RATIOS) - np.log(rr[i])))
            if NICE_RATIOS[j] >= 5 and abs(np.log(NICE_RATIOS[j]) - np.log(rr[i])) < 0.03:
                factor.iloc[i] = 1.0 / NICE_RATIOS[j]
                inferred.iloc[i] = True
    log.info("demergers neutralised: %d", n_dem)
    log.info("corporate actions: %d applied on ex-date, %d shifted <=3 sessions, %d not matched to a price jump, "
             "%d inferred from split-like jumps", n_ok, n_shift, n_drop, int(inferred.sum()))
    return factor, inferred, demerger


def _anchor(px: pd.DataFrame, ret_col: str) -> pd.Series:
    r = px[ret_col].fillna(0.0)
    growth = (1.0 + r).groupby(px["symbol"], sort=False).cumprod()
    last_growth = growth.groupby(px["symbol"], sort=False).transform("last")
    last_close = px.groupby("symbol", sort=False)["close"].transform("last")
    return last_close * growth / last_growth


def validate_prevclose_dividends(px: pd.DataFrame, ca: pd.DataFrame, n: int = 300) -> pd.DataFrame:
    """For large dividend ex-dates, does NSE's PREVCLOSE drop by the dividend (adjusted) or not?"""
    d = ca[(ca.dps > 0)].merge(px[["symbol", "date", "prevclose", "prior_close"]],
                               left_on=["symbol", "ex_date"], right_on=["symbol", "date"])
    d["div_yield"] = d.dps / d.prior_close
    d = d[d.div_yield > 0.02].head(n)
    d["prevclose_equals_prior_close"] = (d.prevclose - d.prior_close).abs() < 0.01 * d.prior_close * 0.1
    return d


if __name__ == "__main__":
    build()
