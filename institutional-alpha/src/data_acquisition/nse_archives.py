"""Download NSE public archive files (nsearchives.nseindia.com).

Datasets (all daily, public, no authentication):
  * bhavcopy  — end-of-day prices/volumes for every NSE security
      legacy  : /content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip  (<= 2024-07-05)
      UDiFF   : /content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip             (>= 2024-07-08)
  * mto       — security-wise delivery position  /archives/equities/mto/MTO_{DDMMYYYY}.DAT
  * pr        — "press release" bundle           /archives/equities/bhavcopy/pr/PR{DDMMYY}.zip
                we keep Bc (corporate actions), Bm (board meetings), MCAP and PE members only
  * indices   — closing values of all NSE indices /content/indices/ind_close_all_{DDMMYYYY}.csv

The www.nseindia.com portal (and its JSON APIs) refuse connections from this environment
(Akamai "Access Denied"); we do not attempt to bypass that. Everything here comes from the
static archive host, which is publicly downloadable.

Run:  python -m src.data_acquisition.nse_archives --datasets bhavcopy,mto,pr,indices
Resumable: existing files are skipped; 404s leave a ".404" marker (holidays).
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from src.common import SETTINGS, ensure_dir, get_logger, path

log = get_logger("nse_archives")
ACQ = SETTINGS["acquisition"]
BASE = ACQ["nse_archive_base"]
UDIFF_DATE = dt.date.fromisoformat(ACQ["udiff_switch_date"])

# Weekend sessions (budget days, special live/DR sessions, Muhurat trading) — tried in addition
# to Mon-Fri. Missing ones simply 404.
SPECIAL_WEEKEND_SESSIONS = [
    "2012-01-07", "2012-03-03", "2012-04-28", "2012-09-08", "2013-05-11", "2014-03-22", "2015-02-28",
    "2013-11-03", "2016-10-30", "2019-10-27", "2020-02-01", "2020-11-14", "2023-11-12",
    "2024-01-20", "2024-03-02", "2024-05-18", "2025-02-01", "2026-02-01", "2026-11-08",
]

_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": ACQ["user_agent"], "Accept": "*/*"})
        _local.s = s
    return s


def trading_calendar_candidates(start: dt.date, end: dt.date) -> list[dt.date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    days += [dt.date.fromisoformat(x) for x in SPECIAL_WEEKEND_SESSIONS
             if start <= dt.date.fromisoformat(x) <= end]
    return sorted(set(days))


def url_and_dest(dataset: str, d: dt.date) -> tuple[str, Path]:
    mon = d.strftime("%b").upper()
    raw = path("raw")
    if dataset == "bhavcopy":
        if d < UDIFF_DATE:
            fn = f"cm{d:%d}{mon}{d:%Y}bhav.csv.zip"
            url = f"{BASE}/content/historical/EQUITIES/{d:%Y}/{mon}/{fn}"
        else:
            fn = f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
            url = f"{BASE}/content/cm/{fn}"
        return url, raw / "bhavcopy" / f"{d:%Y}" / fn
    if dataset == "mto":
        fn = f"MTO_{d:%d%m%Y}.DAT"
        return f"{BASE}/archives/equities/mto/{fn}", raw / "mto" / f"{d:%Y}" / fn
    if dataset == "pr":
        fn = f"PR{d:%d%m%y}.zip"
        return f"{BASE}/archives/equities/bhavcopy/pr/{fn}", raw / "pr" / f"{d:%Y}" / f"{d:%Y%m%d}"
    if dataset == "pr_an":  # announcements only (used to recover board-meeting dates where Bm files are empty)
        fn = f"PR{d:%d%m%y}.zip"
        return f"{BASE}/archives/equities/bhavcopy/pr/{fn}", raw / "pr_an" / f"{d:%Y}" / f"{d:%Y%m%d}"
    if dataset == "indices":
        fn = f"ind_close_all_{d:%d%m%Y}.csv"
        return f"{BASE}/content/indices/{fn}", raw / "indices" / f"{d:%Y}" / fn
    raise ValueError(dataset)


PR_KEEP_PREFIXES = ("bc", "bm", "mcap", "pe_")


def _store_pr(content: bytes, dest_dir: Path, prefixes: tuple = PR_KEEP_PREFIXES) -> None:
    ensure_dir(dest_dir)
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        for info in z.infolist():
            name = info.filename.split("/")[-1]
            if name.lower().startswith(prefixes):
                (dest_dir / name).write_bytes(z.read(info))
    (dest_dir / "_ok").write_text("ok")


def fetch_one(dataset: str, d: dt.date) -> str:
    url, dest = url_and_dest(dataset, d)
    done_marker = dest / "_ok" if dataset in ("pr", "pr_an") else dest
    miss_marker = Path(str(dest) + ".404")
    if done_marker.exists() or miss_marker.exists():
        return "skip"
    sess = _session()
    for attempt in range(ACQ["retries"]):
        try:
            time.sleep(ACQ["min_interval_s"])
            r = sess.get(url, timeout=ACQ["timeout_s"])
            if r.status_code == 404:
                ensure_dir(miss_marker.parent)
                miss_marker.write_text(url)
                return "404"
            if r.status_code == 200 and len(r.content) > 0:
                if dataset == "pr":
                    _store_pr(r.content, dest)
                elif dataset == "pr_an":
                    _store_pr(r.content, dest, ("an",))
                else:
                    ensure_dir(dest.parent)
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    tmp.write_bytes(r.content)
                    tmp.replace(dest)
                return "ok"
            log.warning("HTTP %s for %s", r.status_code, url)
        except (requests.RequestException, zipfile.BadZipFile) as exc:
            log.warning("attempt %d failed for %s: %s", attempt + 1, url, exc)
        time.sleep(2 ** attempt)
    return "fail"


def run(datasets: list[str], start: dt.date, end: dt.date, workers: int) -> dict:
    days = trading_calendar_candidates(start, end)
    jobs = [(ds, d) for d in days for ds in datasets]
    if "indices" in datasets:
        # ind_close_all only exists from mid-2012
        jobs = [(ds, d) for ds, d in jobs if not (ds == "indices" and d < dt.date(2012, 6, 1))]
    log.info("%d jobs (%d candidate dates x %s)", len(jobs), len(days), datasets)
    stats: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_one, ds, d): (ds, d) for ds, d in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            res = f.result()
            key = f"{futs[f][0]}:{res}"
            stats[key] = stats.get(key, 0) + 1
            if i % 500 == 0:
                log.info("progress %d/%d %s", i, len(jobs), stats)
    log.info("done %s", stats)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--datasets", default="bhavcopy,mto,pr,indices")
    ap.add_argument("--start", default=ACQ["start_date"])
    ap.add_argument("--end", default=ACQ["end_date"])
    ap.add_argument("--workers", type=int, default=ACQ["workers"])
    a = ap.parse_args()
    run(a.datasets.split(","), dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end), a.workers)


if __name__ == "__main__":
    main()
