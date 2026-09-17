# Sector-level investment flows for Indian equities

## The short answer

**Nobody publishes bulk deals aggregated by sector.** NSE publishes bulk and
block deals at the *security* level, daily. There is no sector field anywhere in
that feed — a bulk deal record is literally:

```json
{
  "BD_DT_DATE": "01-DEC-2025",
  "BD_SYMBOL": "ASTEC",
  "BD_SCRIP_NAME": "Astec LifeSciences Ltd",
  "BD_CLIENT_NAME": "HRTI PRIVATE LIMITED",
  "BD_BUY_SELL": "BUY",
  "BD_QTY_TRD": 108709,
  "BD_TP_WATP": 826.15
}
```

So there are two genuinely different things you might want, and they need
different sources:

| You want | Source | Grain |
|---|---|---|
| **Institutional money flowing into each sector** | NSDL FPI Monitor (fortnightly sector-wise) | Sector, natively |
| **Large individual transactions, rolled up by sector** | NSE bulk/block deals + a sector master you join yourself | Deal, then aggregate |

The first is a published statistic. The second you have to build. Both are
covered below; the first is implemented and working in this directory.

---

## 1. NSDL FPI Monitor — the only native sector-level flow series

This is the regulatory source. NSDL is the designated depository for FPI
reporting, so these are the numbers SEBI republishes and the financial press
quotes. Published **fortnightly** (as on the 15th and the last day of month).

- Selection page: <https://www.fpi.nsdl.co.in/web/Reports/FPI_Fortnightly_Selection.aspx>
- SEBI's mirror: <https://www.sebi.gov.in/statistics/fpi-investment/fortnightly-sector-wise.html>

Each report gives, per sector:

- **AUC** — assets under custody, the *stock* of FPI holdings
- **Net Investment** — net FPI buying/selling over the fortnight, the *flow*

both in INR Cr and USD Mn, split by asset class (Equity, Debt General Limit,
Debt VRR, Debt-FAR, Hybrid) and by vehicle (held directly, via mutual funds,
via AIFs).

### Use it

```bash
python -m sector_flows.nsdl_fpi_sector --list            # every fortnight available
python -m sector_flows.nsdl_fpi_sector --latest          # headline equity flow per sector
python -m sector_flows.nsdl_fpi_sector --since 2024-01-01 -o flows.csv
```

Output is tidy long-format CSV: `as_on, sector, measure, unit, vehicle,
asset_class, value`.

### Practical notes

- **350 fortnights** are available, going back to 2012.
- **Sector definitions changed.** Reports from 2024 onward use the current
  24-row taxonomy (22 sectors + Sovereign + Others). 2018–2021 use a 35-row
  taxonomy, 2014–2016 a 32-row one. **Do not build a long time series across
  that break** without mapping the old names yourself.
- Report filenames are inconsistent (`Jul312026` but `June302026`), so the
  module always resolves URLs from the dropdown instead of constructing them.
  Don't hardcode filenames.
- The table layout also changed: reports before ~2024 have no vehicle-grouping
  header row, and some vintages quote `colspan` differently. The parser handles
  all of these by reading the header structure rather than assuming offsets.
- 2012–2013 and 2017 reports use layouts the parser does not handle. They are
  skipped with a warning rather than failing the run.

### Verification

The parser is self-checking: NSDL publishes a `Total` column per block, and the
component columns are summed against it. Across 2024–2026 that reconciles
**exactly, on all 192 blocks per report**. 2022 and 2023 reports show 1–2
mismatches out of 192, which are in NSDL's own published figures.

This source is reachable from cloud/CI IPs.

---

## 2. NSE bulk/block deals + your own sector join

Implemented across `nse_client.py`, `sector_master.py` and `bulk_deals.py`.

```bash
python -m sector_flows.bulk_deals --days 30
python -m sector_flows.bulk_deals --from 2026-04-01 --to 2026-09-30 --net
python -m sector_flows.bulk_deals --type block_deals --days 90 -o deals.csv

python -m sector_flows.sector_master RELIANCE TCS ASTEC   # inspect the join
```

Output is a sector table (deal count, distinct symbols, buy/sell/net value in
₹ Cr, share of gross) plus an optional deal-level CSV with the sector attached.
Ranges longer than a year are split automatically across NSE's per-request cap.

Symbols that can't be classified are bucketed under `(unclassified)` and listed
on stderr rather than silently dropped — sector totals always reconcile back to
the raw total.

### The deals feed

```
https://www.nseindia.com/api/historicalOR/bulk-block-short-deals
    ?optionType=bulk_deals      # or block_deals | short_selling
    &from=01-09-2026            # DD-MM-YYYY
    &to=30-09-2026
```

Max one year per call. Returns `{"data": [...]}`.

Note the older `/api/historical/bulk-deals` path that circulates in blog posts
and older code **is dead** — `historicalOR` is the current one.

### The sector join

You need a symbol → sector master. Options, best first:

1. **NSE index constituent CSVs** carry an `Industry` column using the same
   22-sector taxonomy NSDL uses — so aggregates line up with source 1 above.
   `https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv`
   covers ~750 names. Add `ind_niftymicrocap250list.csv` for the tail. Bulk
   deals happen disproportionately in small/micro caps, so coverage of the
   long tail matters more here than it usually would.
2. **BSE per-scrip classification**, which is cloud-reachable when NSE is not:
   `https://api.bseindia.com/BseIndiaAPI/api/ComHeadernew/w?quotetype=EQ&scripcode=500002`
   returns the full four-tier path — `Sector` (macro, e.g. "Industrials"),
   `IndustryNew` (the 22-sector level, e.g. "Capital Goods"), `IGroup`,
   `ISubGroup`. One call per symbol, so cache it; you only need the few hundred
   symbols that actually appear in deals.
   `https://api.bseindia.com/BseIndiaAPI/api/DDLIndustry/w` returns the sector
   list itself, and it is **identical** to NSDL's — the two exchanges share the
   classification.

### Caveats that will bite you

- **Aggregate deal value, not count.** Compute `BD_QTY_TRD × BD_TP_WATP`;
  there is no value field.
- **Bulk deals double-count.** Both sides of a trade are disclosed separately,
  and a broker crossing the 0.5% threshold intraday reports the gross. Netting
  buys against sells per symbol-day is usually what you want.
- **Bulk ≠ institutional.** The 0.5%-of-shares threshold means a bulk deal in a
  small cap can be a retail-sized ticket. Block deals (the separate window,
  ₹10 Cr minimum) are the cleaner institutional signal.
- **This is not a flow measure.** Deals are transfers between two parties, so
  sector totals do not mean money entered the sector — unlike source 1, which
  measures exactly that.

### NSE blocks datacenter IPs

NSE's Akamai edge returns `403 Access Denied` to cloud and CI IPs, including
this repo's GitHub Actions runners — verified, not theoretical. It also
requires a cookie bootstrap (hit `https://www.nseindia.com/` with a browser
User-Agent first, reuse the cookie jar) and a plausible `Referer`. Both are
handled by `nse_client.py`, which raises `NSEBlocked` with an explanation
rather than a bare 403 when the block is what it hit.

If you need this in automation, either run it from a residential/India IP, or
use BSE as the deals source too. NSDL and BSE both answer cloud IPs fine.

**Because of that block, the NSE fetch hop is the one part of this that could
not be exercised end-to-end here.** The endpoint, parameters and record shape
were verified against current library source and a live sample payload, and
everything downstream of the fetch — record parsing, the sector join, netting,
range splitting, aggregation, CSV output — was tested against real payload
shapes and live BSE lookups. Expect the first run from an unblocked network to
be the real test of the fetch itself.

---

## 3. What NSE publishes systematically

Endpoints below are under `https://www.nseindia.com/api/` (live/JSON) or
`https://nsearchives.nseindia.com/` (archive files). All need the cookie +
User-Agent handshake above.

**Deals and disclosures**
- `historicalOR/bulk-block-short-deals` — bulk, block and short-selling deals
- `block-deal` — current-session block deal window

**Daily end-of-day files** (the systematic backbone)
- `content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip` — equity bhavcopy
- `content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip` — F&O bhavcopy
- `content/cm/NSE_CM_security_{ddmmyyyy}.csv.gz` — delivery quantity/percentage
- `content/indices/ind_close_all_{ddmmyyyy}.csv` — all index closes
- `archives/equities/bhavcopy/pr/PR{ddmmyy}.zip` — the full daily price report
  bundle (includes the day's bulk deals, price bands, corporate actions)
- `api/daily-reports?key=favCapital` — the machine-readable index of the above

**Corporate data**
- `corporate-announcements`, `corporate-board-meetings`
- `corporates-corporateActions` — splits, bonuses, dividends
- `corporates-financial-results`, `results-comparision`
- `corporate-share-holdings-master` — **shareholding patterns**, the quarterly
  promoter/FII/DII/public split per company. This is your other route to
  institutional ownership by sector, quarterly rather than fortnightly.
- `annual-reports`, `circulars`

**Reference / masters**
- `content/equities/sec_list_{ddmmyyyy}.csv` — securities master
- `equity-stockIndices?index=NIFTY%20500` — index constituents
- `index-names`, `allIndices`, `underlying-information`, `holiday-master`

**Live market**
- `quote-equity`, `equity-stockIndices-adu` (advance/decline),
  `live-analysis-volume-gainers`, `option-chain-v3`, `marketStatus`

**Historical series**
- `historicalOR/indicesHistory`, `historicalOR/vixhistory`, `historicalOR/foCPV`

**Participant-wise flows** — NSE also publishes daily FII/DII/Pro/Client
activity under `content/nsccl/` (`fao_participant_wise_trading_volume_*.csv`,
`fao_participant_wise_oi_*.csv`). These are *categories of participant*, not
sectors. Filenames not verified here because of the IP block above — confirm
against the daily reports index before relying on them.

---

## Recommendation

For "sector-level bulk investments", use **source 1 (NSDL)**. It measures what
the phrase actually implies — money moving into sectors — it is the regulatory
source, it needs no joins or assumptions, and it works from CI. Reach for
source 2 only if you specifically need individual large tickets and who
transacted them, and treat its sector totals as transaction volume rather than
as flow.
