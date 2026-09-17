"""Fetch NSE bulk/block deals and roll them up by sector.

NSE publishes bulk deals, block deals and short-selling disclosures daily, at
the *security* level -- there is no sector field in the feed. This module
fetches the deal records and joins them against
:mod:`sector_flows.sector_master` to produce sector aggregates.

**Read this before trusting the output:**

* **Deals are not flows.** Every deal has a buyer and a seller, both of whom may
  disclose. A sector total here is transaction *volume*, not money entering the
  sector. For actual sector flows use :mod:`sector_flows.nsdl_fpi_sector`.
* **Bulk deals double-count.** Both sides are disclosed separately, and the
  0.5% threshold is measured on a broker's aggregate intraday activity, so a
  single position can appear as several records. ``--net`` reports buy minus
  sell, which is usually what you want.
* **Bulk is not a synonym for institutional.** The threshold is 0.5% of a
  company's shares, so a bulk deal in a micro cap can be a small ticket. Block
  deals (separate window, Rs 10 Cr minimum) are the cleaner institutional read.
* **Value is derived**, as quantity x weighted-average traded price. The feed
  carries no value field.

Usage::

    python -m sector_flows.bulk_deals --days 30
    python -m sector_flows.bulk_deals --from 2026-04-01 --to 2026-09-30 --net
    python -m sector_flows.bulk_deals --type block_deals --days 90 -o deals.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

from .nse_client import NSEBlocked, NSEClient
from .sector_master import Classification, SectorMaster

DEALS_PATH = "historicalOR/bulk-block-short-deals"
REFERER = "https://www.nseindia.com/report-detail/display-bulk-and-block-deals"

DEAL_TYPES = ("bulk_deals", "block_deals", "short_selling")

# NSE caps a single request at one year.
MAX_WINDOW = timedelta(days=365)

UNCLASSIFIED = "(unclassified)"


@dataclass(frozen=True)
class Deal:
    """One disclosed deal, with its sector attached where resolvable."""

    date: date
    symbol: str
    security: str
    client: str
    side: str  # "BUY" | "SELL"
    quantity: float
    price: float
    sector: str = UNCLASSIFIED
    macro: str = ""

    @property
    def value(self) -> float:
        """Traded value in rupees (quantity x weighted-average price)."""
        return self.quantity * self.price

    @property
    def signed_value(self) -> float:
        return self.value if self.side.upper().startswith("B") else -self.value


def _parse_date(raw: str) -> date | None:
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _num(raw) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def _windows(start: date, end: date) -> Iterable[tuple[date, date]]:
    """Split a range into chunks NSE will accept."""
    while start <= end:
        stop = min(start + MAX_WINDOW - timedelta(days=1), end)
        yield start, stop
        start = stop + timedelta(days=1)


def fetch_deals(
    start: date,
    end: date,
    deal_type: str = "bulk_deals",
    client: NSEClient | None = None,
) -> list[Deal]:
    """Fetch raw deal records for a date range, without sectors attached."""
    if deal_type not in DEAL_TYPES:
        raise ValueError(f"deal_type must be one of {DEAL_TYPES}")
    if start > end:
        raise ValueError("start date must not be after end date")

    client = client or NSEClient()
    deals: list[Deal] = []

    for win_start, win_end in _windows(start, end):
        payload = client.get_json(
            DEALS_PATH,
            params={
                "optionType": deal_type,
                "from": f"{win_start:%d-%m-%Y}",
                "to": f"{win_end:%d-%m-%Y}",
            },
            referer=REFERER,
        )
        records = payload.get("data", []) if isinstance(payload, dict) else payload
        for r in records or []:
            traded_on = _parse_date(str(r.get("BD_DT_DATE", "")))
            symbol = (r.get("BD_SYMBOL") or "").strip().upper()
            if not traded_on or not symbol:
                continue
            deals.append(
                Deal(
                    date=traded_on,
                    symbol=symbol,
                    security=(r.get("BD_SCRIP_NAME") or "").strip(),
                    client=(r.get("BD_CLIENT_NAME") or "").strip(),
                    side=(r.get("BD_BUY_SELL") or "").strip(),
                    quantity=_num(r.get("BD_QTY_TRD")),
                    price=_num(r.get("BD_TP_WATP")),
                )
            )

    deals.sort(key=lambda d: (d.date, d.symbol))
    return deals


def attach_sectors(
    deals: Sequence[Deal], master: SectorMaster | None = None
) -> tuple[list[Deal], list[str]]:
    """Join deals to sectors.

    Returns the enriched deals and the symbols that could not be classified.
    Unresolved symbols are kept under ``(unclassified)`` rather than dropped, so
    sector totals always reconcile back to the raw total.
    """
    master = master or SectorMaster()
    lookups = master.lookup_many((d.symbol, d.security) for d in deals)

    enriched: list[Deal] = []
    unresolved: set[str] = set()
    for d in deals:
        c: Classification | None = lookups.get(d.symbol)
        if c is None or not c.sector:
            unresolved.add(d.symbol)
            enriched.append(d)
        else:
            enriched.append(
                Deal(**{**d.__dict__, "sector": c.sector, "macro": c.macro})
            )
    return enriched, sorted(unresolved)


@dataclass
class SectorTotal:
    sector: str
    deals: int = 0
    symbols: int = 0
    buy_value: float = 0.0
    sell_value: float = 0.0

    @property
    def gross_value(self) -> float:
        return self.buy_value + self.sell_value

    @property
    def net_value(self) -> float:
        return self.buy_value - self.sell_value


def aggregate(deals: Sequence[Deal]) -> list[SectorTotal]:
    """Roll deals up to sector totals, sorted by gross value."""
    buckets: dict[str, SectorTotal] = {}
    symbols: dict[str, set[str]] = defaultdict(set)

    for d in deals:
        t = buckets.setdefault(d.sector, SectorTotal(d.sector))
        t.deals += 1
        symbols[d.sector].add(d.symbol)
        if d.side.upper().startswith("B"):
            t.buy_value += d.value
        else:
            t.sell_value += d.value

    for sector, t in buckets.items():
        t.symbols = len(symbols[sector])

    return sorted(buckets.values(), key=lambda t: t.gross_value, reverse=True)


def write_deals_csv(deals: Sequence[Deal], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "date", "symbol", "security", "sector", "macro",
                "client", "side", "quantity", "price", "value",
            ]
        )
        for d in deals:
            w.writerow(
                [
                    f"{d.date:%Y-%m-%d}", d.symbol, d.security, d.sector, d.macro,
                    d.client, d.side, d.quantity, d.price, round(d.value, 2),
                ]
            )


def _crore(x: float) -> float:
    return x / 1e7


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--type", dest="deal_type", choices=DEAL_TYPES, default="bulk_deals"
    )
    p.add_argument("--days", type=int, help="look back this many days from today")
    p.add_argument("--from", dest="start", metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="end", metavar="YYYY-MM-DD")
    p.add_argument(
        "--net",
        action="store_true",
        help="rank sectors by net (buy - sell) instead of gross value",
    )
    p.add_argument(
        "--backend", choices=("auto", "nse", "bse"), default="auto",
        help="sector master backend",
    )
    p.add_argument("-o", "--out", metavar="CSV", help="write deal-level CSV here")
    args = p.parse_args(argv)

    if args.days:
        end = date.today()
        start = end - timedelta(days=args.days)
    elif args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = (
            datetime.strptime(args.end, "%Y-%m-%d").date()
            if args.end
            else date.today()
        )
    else:
        p.error("pass --days, or --from (with optional --to)")

    print(
        f"Fetching {args.deal_type} from {start:%d-%m-%Y} to {end:%d-%m-%Y}...",
        file=sys.stderr,
    )
    try:
        deals = fetch_deals(start, end, args.deal_type)
    except NSEBlocked as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    if not deals:
        print("No deals returned for that range.", file=sys.stderr)
        return 1
    print(f"{len(deals)} deal records.", file=sys.stderr)

    deals, unresolved = attach_sectors(deals, SectorMaster(backend=args.backend))
    totals = aggregate(deals)
    if args.net:
        totals.sort(key=lambda t: t.net_value, reverse=True)

    grand = sum(t.gross_value for t in totals)
    label = f"{args.deal_type.replace('_', ' ')}, {start:%d-%b-%Y} to {end:%d-%b-%Y}"
    print(f"\n{label}\n")
    print(
        f"{'Sector':<40}{'Deals':>7}{'Syms':>6}"
        f"{'Buy (Cr)':>13}{'Sell (Cr)':>12}{'Net (Cr)':>12}{'Gross %':>9}"
    )
    print("-" * 99)
    for t in totals:
        share = 100 * t.gross_value / grand if grand else 0
        print(
            f"{t.sector[:39]:<40}{t.deals:>7}{t.symbols:>6}"
            f"{_crore(t.buy_value):>13,.1f}{_crore(t.sell_value):>12,.1f}"
            f"{_crore(t.net_value):>12,.1f}{share:>8.1f}%"
        )
    print("-" * 99)
    print(f"{'TOTAL':<40}{len(deals):>7}{'':>6}{_crore(grand):>13,.1f}")

    if unresolved:
        print(
            f"\n{len(unresolved)} symbol(s) had no sector and are grouped under "
            f"{UNCLASSIFIED}:\n  {', '.join(unresolved[:20])}"
            + (" ..." if len(unresolved) > 20 else ""),
            file=sys.stderr,
        )

    if args.out:
        write_deals_csv(deals, args.out)
        print(f"\nWrote {len(deals)} deal rows -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
