"""Fortnightly sector-wise FPI investment data, from NSDL's FPI Monitor.

This is the only *natively* sector-level institutional flow series published for
Indian equities. NSDL is the designated depository for FPI reporting, so the
numbers here are the regulatory source that SEBI and the financial press quote.

Two measures per fortnight, for each of ~22 sectors:

* AUC           -- assets under custody (the stock of FPI holdings)
* Net Investment -- net FPI buying/selling over the fortnight (the flow)

Each is published in both INR Cr and USD Mn, split by asset class
(Equity, Debt General Limit, Debt VRR, Debt-FAR, Hybrid, ... , Total).

Reports live as static HTML, one file per fortnight, listed in a dropdown on
the selection page. Filenames are *not* consistently formatted
("Jul312026" vs "June302026"), so always resolve them from the dropdown
rather than constructing them.

Usage::

    python -m sector_flows.nsdl_fpi_sector --list
    python -m sector_flows.nsdl_fpi_sector --latest -o auc.csv
    python -m sector_flows.nsdl_fpi_sector --since 2026-01-01 -o flows.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from typing import Iterable, Iterator, Sequence

import requests

BASE = "https://www.fpi.nsdl.co.in/web"
SELECTION_URL = f"{BASE}/Reports/FPI_Fortnightly_Selection.aspx"

# NSDL serves these fine from cloud IPs, but still wants a browser-ish UA.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 45


@dataclass(frozen=True)
class Report:
    """One fortnightly report: its as-on date and absolute URL."""

    as_on: date
    url: str

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.as_on:%Y-%m-%d}  {self.url}"


@dataclass(frozen=True)
class Row:
    """One tidy observation.

    ``vehicle`` disambiguates the two columns both labelled "Equity": direct FPI
    equity holdings versus FPI money held through mutual funds.
    """

    as_on: date
    sector: str
    measure: str  # e.g. "AUC as on August 31, 2026"
    unit: str  # "IN INR Cr." | "IN USD Mn"
    vehicle: str  # "Direct" | "Mutual Funds" | "AIF" | "Total"
    asset_class: str  # "Equity" | "Debt General Limit" | ... | "Total"
    value: float


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _text(fragment: str) -> str:
    """Strip tags/entities from an HTML fragment and collapse whitespace."""
    return unescape(re.sub(r"<[^>]+>", "", fragment)).replace("\xa0", " ").strip()


def _parse_amount(raw: str) -> float | None:
    """Parse an Indian-grouped number ('5,57,414', '-1,299', '(12)') to float."""
    s = raw.strip().replace(",", "")
    if not s or s in {"-", "--"}:
        return None
    if s.startswith("(") and s.endswith(")"):  # accounting negatives
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y")


def _parse_as_on(label: str) -> date | None:
    """'AUG 31, 2026' / 'JUNE 30, 2026' -> date."""
    cleaned = re.sub(r"\s+", " ", label).strip().title()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def list_reports(session: requests.Session | None = None) -> list[Report]:
    """Return every published fortnightly report, newest first."""
    session = session or _session()
    resp = session.get(SELECTION_URL, timeout=TIMEOUT)
    resp.raise_for_status()

    reports: list[Report] = []
    seen: set[date] = set()
    for value, label in re.findall(
        r"<option[^>]*value=\"([^\"]+)\"[^>]*>([^<]*)</option>", resp.text, re.I
    ):
        if "Fortnightly_Sector_wise" not in value:
            continue
        as_on = _parse_as_on(_text(label))
        if as_on is None or as_on in seen:
            continue
        seen.add(as_on)
        reports.append(Report(as_on, f"{BASE}/{value.lstrip('~/')}"))

    if not reports:
        raise RuntimeError(
            f"No fortnightly reports found at {SELECTION_URL}; the page layout "
            "may have changed."
        )
    reports.sort(key=lambda r: r.as_on, reverse=True)
    return reports


def _table_rows(html: str) -> list[list[str]]:
    """Extract table rows, expanding each cell across its ``colspan``.

    The report's header is three levels deep and relies entirely on colspans, so
    expanding them is what lets every header row line up with the data grid.
    """
    rows: list[list[str]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells: list[str] = []
        for attrs, body in re.findall(
            r"<t[dh]([^>]*)>(.*?)</t[dh]>", tr, re.S | re.I
        ):
            # Vintages differ: colspan="3", colspan='3' and bare colspan=3 all occur.
            m = re.search(r"colspan\s*=\s*['\"]?(\d+)", attrs, re.I)
            span = int(m.group(1)) if m else 1
            cells.extend([_text(body)] * span)
        if cells:
            rows.append(cells)
    return rows


def _forward_fill(cells: Sequence[str], start: int) -> list[str]:
    """Carry each label rightwards over the blank cells that follow it."""
    out = list(cells)
    last = ""
    for i in range(start, len(out)):
        if out[i]:
            last = out[i]
        else:
            out[i] = last
    return out


# The 12 columns inside each measure/unit block are grouped by investment
# vehicle in the third header row. Normalise those labels.
_VEHICLES = {
    "mutual funds": "Mutual Funds",
    "alternative investment funds (aifs)": "AIF",
}


def parse_report(html: str, as_on: date) -> list[Row]:
    """Parse one fortnightly report into tidy rows.

    The header is built entirely with colspans, and its depth varies by vintage:

    1. measure groups -- "AUC as on ..." / "Net Investment ..."
    2. currency units -- "IN INR Cr." / "IN USD Mn"
    3. vehicle groups -- Equity / Debt / Hybrid / Mutual Funds / AIF
       (reports before ~2024 predate pooled vehicles and omit this row)
    4. leaf columns   -- "Sr. No.", "Sectors", then the asset classes

    Expanding colspans puts every header row on the same grid, so each data
    column is labelled by its full header path rather than by fixed offsets.
    Rows are identified by content, so the missing third row is handled too.
    """
    rows = _table_rows(html)

    header_idx = next(
        (
            i
            for i, c in enumerate(rows)
            if c and c[0].strip().lower().startswith("sr. no")
        ),
        -1,
    )
    if header_idx < 2:
        raise RuntimeError(
            "Could not locate the sector table header; NSDL may have changed "
            "the report layout."
        )

    leaf = rows[header_idx]
    width = len(leaf)
    above = [_forward_fill(r, 2) for r in rows[:header_idx] if len(r) == width]

    def _find(pred) -> list[str] | None:
        return next((r for r in reversed(above) if pred(r)), None)

    measures = _find(lambda r: any(c.lower().startswith("auc") for c in r))
    units = _find(lambda r: any(c.startswith("IN ") for c in r))
    # Whatever sits between the units row and the leaf row groups by vehicle.
    vehicles = _find(
        lambda r: r is not measures and r is not units and any(r[2:])
    )

    if measures is None or units is None:
        raise RuntimeError(
            "Header rows do not align with the data grid; NSDL may have changed "
            "the report layout."
        )

    out: list[Row] = []
    for cells in rows[header_idx + 1 :]:
        # Data rows are numbered; this skips totals, footnotes and spacers.
        if len(cells) != width or not cells[0].strip().isdigit():
            continue
        sector = cells[1].strip()
        if not sector:
            continue
        for i in range(2, width):
            value = _parse_amount(cells[i])
            if value is None:
                continue
            asset_class = leaf[i] or "Total"
            if asset_class == "Total":
                # Closes each block and sits outside the vehicle groups above it,
                # so the forward fill would otherwise tag it with the last one.
                vehicle = "Total"
            elif vehicles is None:
                vehicle = "Direct"  # older reports cover direct holdings only
            else:
                vehicle = vehicles[i].strip()
                vehicle = _VEHICLES.get(vehicle.lower(), vehicle or "Total")
                if vehicle in {"Equity", "Debt", "Hybrid"}:
                    vehicle = "Direct"  # held directly, not through a fund
            out.append(
                Row(as_on, sector, measures[i], units[i], vehicle, asset_class, value)
            )

    if not out:
        raise RuntimeError("Sector table parsed but produced no rows.")
    return out


def fetch(report: Report, session: requests.Session | None = None) -> list[Row]:
    """Download and parse a single fortnightly report."""
    session = session or _session()
    resp = session.get(report.url, timeout=TIMEOUT)
    resp.raise_for_status()
    return parse_report(resp.text, report.as_on)


def fetch_many(
    reports: Iterable[Report], session: requests.Session | None = None
) -> Iterator[Row]:
    """Download several reports, yielding rows as each one lands."""
    session = session or _session()
    for report in reports:
        try:
            yield from fetch(report, session)
        except Exception as exc:  # keep going; one bad fortnight shouldn't abort
            print(f"  ! {report.as_on:%Y-%m-%d}: {exc}", file=sys.stderr)


def write_csv(rows: Sequence[Row], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["as_on", "sector", "measure", "unit", "vehicle", "asset_class", "value"]
        )
        for r in rows:
            w.writerow(
                [
                    f"{r.as_on:%Y-%m-%d}",
                    r.sector,
                    r.measure,
                    r.unit,
                    r.vehicle,
                    r.asset_class,
                    r.value,
                ]
            )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--list", action="store_true", help="list available fortnights")
    p.add_argument("--latest", action="store_true", help="fetch the newest fortnight")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="fetch every fortnight since")
    p.add_argument("-o", "--out", metavar="CSV", help="write tidy CSV here")
    args = p.parse_args(argv)

    session = _session()
    reports = list_reports(session)

    if args.list:
        for r in reports:
            print(r)
        return 0

    if args.since:
        cutoff = datetime.strptime(args.since, "%Y-%m-%d").date()
        wanted = [r for r in reports if r.as_on >= cutoff]
    else:
        wanted = reports[:1]  # --latest, and the default

    if not wanted:
        print("No reports match.", file=sys.stderr)
        return 1

    print(f"Fetching {len(wanted)} report(s)...", file=sys.stderr)
    rows = list(fetch_many(wanted, session))

    if args.out:
        write_csv(rows, args.out)
        print(f"Wrote {len(rows)} rows -> {args.out}", file=sys.stderr)
    else:
        # Default view: the headline direct-equity net flow per sector, INR Cr.
        latest = max(r.measure for r in rows if r.measure.lower().startswith("net"))
        print(f"\n{latest}  (INR Cr, direct FPI equity)\n")
        for r in rows:
            if (
                r.vehicle == "Direct"
                and r.asset_class == "Equity"
                and r.unit.startswith("IN INR")
                and r.measure == latest
            ):
                print(f"  {r.sector:<45} {r.value:>12,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
