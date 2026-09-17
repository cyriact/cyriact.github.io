"""Symbol -> sector mapping, for rolling security-level data up to sectors.

Neither exchange ships a single file mapping every listed symbol to a sector,
so this builds one from whichever backend is reachable:

``nse``
    NSE index constituent CSVs, which carry an ``Industry`` column. Covers the
    Total Market index (~750 names) plus Microcap 250. Fast -- two file
    downloads -- but blocked from datacenter IPs, and it only covers index
    members, which matters because bulk deals skew to the small-cap tail.

``bse``
    BSE's scrip master plus a per-scrip classification lookup. Answers cloud
    IPs, and covers every listed company (~5,000), but costs one HTTP call per
    symbol, so results are cached on disk.

Both use the same 22-sector taxonomy that NSDL uses in its sector-wise FPI
reports, so aggregates built here line up with :mod:`sector_flows.nsdl_fpi_sector`.

Usage::

    python -m sector_flows.sector_master RELIANCE TCS ASTEC
    python -m sector_flows.sector_master --backend nse --dump sectors.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

from .nse_client import ARCHIVES, USER_AGENT, NSEClient

CACHE_DIR = Path(__file__).with_name(".cache")

BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json, text/plain, */*",
}

# NSE index constituent files, widest coverage first.
NSE_CONSTITUENT_FILES = (
    "ind_niftytotalmarket_list.csv",
    "ind_niftymicrocap250list.csv",
)

TIMEOUT = 45


@dataclass(frozen=True)
class Classification:
    """Where one symbol sits in the exchange's industry hierarchy."""

    symbol: str
    name: str = ""
    macro: str = ""  # e.g. "Commodities"
    sector: str = ""  # the 22-sector level, e.g. "Chemicals"
    industry: str = ""
    basic_industry: str = ""
    source: str = ""


def _normalise_name(name: str) -> str:
    """Reduce a company name to a comparable key.

    Deal feeds and scrip masters spell the same company differently
    ('Astec LifeSciences Ltd' vs 'ASTEC LIFESCIENCES LTD.'), so drop
    punctuation, the usual suffixes and all spacing before comparing.
    """
    s = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    s = re.sub(
        r"\b(limited|ltd|private|pvt|public|company|co|corporation|corp|"
        r"industries|india|the)\b",
        " ",
        s,
    )
    return re.sub(r"\s+", "", s)


class SectorMaster:
    """Resolves symbols to sectors, with an on-disk cache."""

    def __init__(self, backend: str = "auto", cache_dir: Path | None = None) -> None:
        self.backend = backend
        self.cache_dir = cache_dir or CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._by_symbol: dict[str, Classification] = {}
        self._by_name: dict[str, Classification] = {}
        self._bse_scrips: dict[str, dict] | None = None
        self._loaded = False
        self._resolved_backend = ""

    # -- cache ---------------------------------------------------------------

    @property
    def _cache_file(self) -> Path:
        return self.cache_dir / f"sectors_{self._resolved_backend or self.backend}.json"

    def _load_cache(self) -> None:
        if not self._cache_file.exists():
            return
        try:
            raw = json.loads(self._cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for d in raw.values():
            self._index(Classification(**d))

    def save_cache(self) -> None:
        payload = {c.symbol: c.__dict__ for c in self._by_symbol.values()}
        self._cache_file.write_text(json.dumps(payload, indent=0), encoding="utf-8")

    def _index(self, c: Classification) -> None:
        self._by_symbol[c.symbol.upper()] = c
        if c.name:
            self._by_name.setdefault(_normalise_name(c.name), c)

    # -- NSE backend ---------------------------------------------------------

    def _load_nse(self) -> int:
        client = NSEClient()
        loaded = 0
        for filename in NSE_CONSTITUENT_FILES:
            url = f"{ARCHIVES}/content/indices/{filename}"
            resp = client.session.get(
                url, timeout=TIMEOUT, headers={"Referer": "https://www.nseindia.com/"}
            )
            resp.raise_for_status()
            for row in csv.DictReader(io.StringIO(resp.text)):
                row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
                symbol = row.get("Symbol", "")
                if not symbol:
                    continue
                self._index(
                    Classification(
                        symbol=symbol.upper(),
                        name=row.get("Company Name", ""),
                        sector=row.get("Industry", ""),
                        source=f"nse:{filename}",
                    )
                )
                loaded += 1
        return loaded

    # -- BSE backend ---------------------------------------------------------

    def _load_bse_scrips(self) -> dict[str, dict]:
        if self._bse_scrips is not None:
            return self._bse_scrips
        url = (
            f"{BSE_API}/ListofScripData/w"
            "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
        )
        resp = requests.get(url, headers=BSE_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        scrips: dict[str, dict] = {}
        for s in resp.json():
            sid = (s.get("scrip_id") or "").strip().upper()
            if sid:
                scrips[sid] = s
            name_key = _normalise_name(s.get("Scrip_Name") or "")
            if name_key:
                scrips.setdefault(f"name::{name_key}", s)
        self._bse_scrips = scrips
        return scrips

    def _classify_bse(self, symbol: str, name: str = "") -> Classification | None:
        """Look one symbol up through BSE's per-scrip classification endpoint."""
        scrips = self._load_bse_scrips()
        scrip = scrips.get(symbol.upper())
        if scrip is None and name:
            scrip = scrips.get(f"name::{_normalise_name(name)}")
        if scrip is None:
            return None

        url = (
            f"{BSE_API}/ComHeadernew/w"
            f"?quotetype=EQ&scripcode={scrip['SCRIP_CD']}&seriesid="
        )
        try:
            resp = requests.get(url, headers=BSE_HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            d = resp.json()
        except (requests.RequestException, ValueError):
            return None

        return Classification(
            symbol=symbol.upper(),
            name=scrip.get("Scrip_Name", ""),
            macro=(d.get("Sector") or "").strip(),
            sector=(d.get("IndustryNew") or "").strip(),
            industry=(d.get("IGroup") or "").strip(),
            basic_industry=(d.get("ISubGroup") or "").strip(),
            source="bse",
        )

    # -- public API ----------------------------------------------------------

    def load(self) -> None:
        """Populate the map, choosing a backend and falling back if needed."""
        if self._loaded:
            return

        if self.backend in ("auto", "nse"):
            try:
                self._resolved_backend = "nse"
                self._load_cache()
                if not self._by_symbol:
                    n = self._load_nse()
                    print(f"Loaded {n} symbols from NSE constituents.", file=sys.stderr)
                    self.save_cache()
                self._loaded = True
                return
            except Exception as exc:
                if self.backend == "nse":
                    raise
                print(
                    f"NSE sector master unavailable ({type(exc).__name__}); "
                    "falling back to BSE.",
                    file=sys.stderr,
                )
                self._by_symbol.clear()
                self._by_name.clear()

        self._resolved_backend = "bse"
        self._load_cache()
        self._loaded = True

    def lookup(self, symbol: str, name: str = "") -> Classification | None:
        """Resolve one symbol, consulting the cache and then the backend."""
        self.load()
        key = symbol.upper()
        if key in self._by_symbol:
            return self._by_symbol[key]
        if name:
            hit = self._by_name.get(_normalise_name(name))
            if hit:
                return hit
        if self._resolved_backend == "bse":
            c = self._classify_bse(symbol, name)
            if c:
                self._index(c)
                return c
        return None

    def lookup_many(
        self, pairs: Iterable[tuple[str, str]], pause: float = 0.15
    ) -> dict[str, Classification]:
        """Resolve many ``(symbol, name)`` pairs, caching as it goes.

        BSE is queried one symbol at a time, so this is paced and the cache is
        flushed as results arrive -- an interrupted run keeps its progress.
        """
        self.load()
        out: dict[str, Classification] = {}
        pairs = list(dict.fromkeys((s.upper(), n) for s, n in pairs))
        pending = [(s, n) for s, n in pairs if s not in self._by_symbol]
        if pending and self._resolved_backend == "bse":
            print(
                f"Classifying {len(pending)} new symbol(s) via BSE...", file=sys.stderr
            )

        for i, (symbol, name) in enumerate(pairs, 1):
            c = self.lookup(symbol, name)
            if c:
                out[symbol] = c
            if symbol in {p[0] for p in pending}:
                if i % 25 == 0:
                    self.save_cache()
                time.sleep(pause)

        self.save_cache()
        return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("symbols", nargs="*", help="symbols to classify")
    p.add_argument(
        "--backend", choices=("auto", "nse", "bse"), default="auto", help="data source"
    )
    p.add_argument("--dump", metavar="CSV", help="write the whole map to CSV")
    args = p.parse_args(argv)

    master = SectorMaster(backend=args.backend)
    master.load()

    if args.dump:
        with open(args.dump, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["symbol", "name", "macro", "sector", "industry", "source"])
            for c in sorted(master._by_symbol.values(), key=lambda x: x.symbol):
                w.writerow(
                    [c.symbol, c.name, c.macro, c.sector, c.industry, c.source]
                )
        print(f"Wrote {len(master._by_symbol)} rows -> {args.dump}", file=sys.stderr)

    for symbol in args.symbols:
        c = master.lookup(symbol)
        if c:
            print(f"{c.symbol:<14} {c.sector:<38} {c.macro:<22} [{c.source}]")
        else:
            print(f"{symbol:<14} -- not found --")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
