"""Point-in-time shareholding panel from parsed SHP XBRL filings.

* Canonical ownership categories across the 2018 / 2019 / 2020 / 2022 / 2025 SEBI taxonomies.
* Availability date = public broadcast date (revised filings: original submission date unless
  settings.shareholding.use_revision_broadcast_date). Signals may only use a filing from the first
  trading session strictly AFTER that date (`effective_date`).
* Quarter-on-quarter changes in percentage points AND share-count based net flows, adjusted for
  splits/bonuses (via NSE corporate-action ratios) and flagged for issuance/dilution events
  (IPO quarter, QIP, rights, mergers) so that a %-change is not mistaken for buying.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.common import SETTINGS, ensure_dir, get_logger, path, register_dataset
from src.investor_classification.classifier import (CATEGORY_QUALITY, CREDIBLE_CATEGORIES, classify_frame,
                                                    is_institutional_axis)

log = get_logger("shareholding")

# canonical category -> list of XBRL members (case-insensitive) that are SUMMED
CATS = {
    "promoter": ["ShareholdingOfPromoterAndPromoterGroupMember"],
    "public": ["PublicShareholdingMember"],
    "total": ["ShareholdingPatternMember"],
    "mf": ["MutualFundsOrUtiMember"],
    "insurance": ["InsuranceCompaniesMember"],
    "banks": ["FinancialInstitutionOrBanksMember", "BanksMember"],
    "pension": ["ProvidentFundsOrPensionFundsMember"],
    "aif": ["AlternativeInvestmentFundsMember"],
    "nbfc": ["NBFCsRegisteredWithRbiMember"],
    "swf_domestic": ["SovereignWealthFundsDomesticMember"],
    "fpi_old": ["InstitutionsForeignPortfolioInvestorMember"],
    "fpi_cat1": ["InstitutionsForeignPortfolioInvestorCategoryOneMember",
                 "InstitutionsForeignPortfolioInvestorCatergoryOneMember"],
    "fpi_cat2": ["InstitutionsForeignPortfolioInvestorCategoryTwoMember",
                 "InstitutionsForeignPortfolioInvestorCatergoryTwoMember"],
    "fvci": ["ForeignVentureCapitalInvestorsMember"],
    "fdi_public": ["ForeignDirectInvestmentMember"],
    "swf_foreign": ["SovereignWealthFundsForeignMember"],
    "inst_old": ["InstitutionsMember"],
    "inst_domestic_new": ["InstitutionsDomesticMember"],
    "inst_foreign_new": ["InstitutionsForeignMember"],
    "noninst": ["NonInstitutionsMember"],
}
_MEMBER_TO_CAT = {m.lower(): c for c, ms in CATS.items() for m in ms}


def _availability(fil: pd.DataFrame) -> pd.Series:
    use_rev = SETTINGS["shareholding"]["use_revision_broadcast_date"]
    bd = pd.to_datetime(fil["broadcast_ts"]).dt.normalize()
    fd = pd.to_datetime(fil["filename_date"])
    sd = pd.to_datetime(fil["submission_date"])
    primary = bd.fillna(fd)
    if not use_rev:
        primary = np.where(fil["revised"] & sd.notna(), sd, primary)
        primary = pd.to_datetime(pd.Series(primary, index=fil.index))
    fallback = pd.to_datetime(fil["period_end"]) + pd.Timedelta(days=30)
    return primary.fillna(sd).fillna(fallback)


def next_session(dates: pd.Series, calendar: pd.DatetimeIndex) -> pd.Series:
    """First trading session strictly after each date."""
    idx = calendar.searchsorted(pd.to_datetime(dates).values, side="right")
    idx = np.clip(idx, 0, len(calendar) - 1)
    out = pd.Series(calendar[idx], index=dates.index)
    out[pd.to_datetime(dates).values >= calendar[-1].to_datetime64()] = pd.NaT
    return out


def build(calendar: pd.DatetimeIndex, prices: pd.DataFrame | None = None) -> pd.DataFrame:
    proc = path("processed")
    fil = pd.read_parquet(proc / "shp_filings.parquet")
    cats = pd.read_parquet(proc / "shp_categories.parquet")
    fil = fil.dropna(subset=["total_shares"]).copy()
    fil["period_end"] = pd.to_datetime(fil["period_end"])
    fil["avail_date"] = _availability(fil)
    fil["effective_date"] = next_session(fil["avail_date"], calendar)

    cats["cat"] = cats["member"].str.lower().map(_MEMBER_TO_CAT)
    cats = cats.dropna(subset=["cat"])
    wide_pct = cats.pivot_table(index="filename", columns="cat", values="pct", aggfunc="sum")
    wide_sh = cats.pivot_table(index="filename", columns="cat", values="shares", aggfunc="sum")
    w = pd.DataFrame(index=wide_pct.index)
    g = lambda df, c: df[c] if c in df.columns else pd.Series(np.nan, index=df.index)  # noqa: E731
    for src, name in ((wide_pct, "pct"), (wide_sh, "sh")):
        fpi_new = g(src, "fpi_cat1").fillna(0) + g(src, "fpi_cat2").fillna(0)
        has_new = g(src, "inst_foreign_new").notna()
        fpi = np.where(has_new, fpi_new, g(src, "fpi_old"))
        fii = np.where(has_new, fpi_new + g(src, "swf_foreign").fillna(0), g(src, "fpi_old"))
        # Harmonised across SEBI taxonomies: the 2022+ formats moved FDI (strategic foreign holders) and
        # NBFCs into the institutions tables, which would create artificial jumps at the switch.
        # foreign institutions = FPI + FVCI (+ foreign SWF line in 2022+); domestic institutions exclude NBFCs.
        fvci = g(src, "fvci").fillna(0)
        foreign_inst = np.where(has_new, fpi_new + fvci + g(src, "swf_foreign").fillna(0),
                                g(src, "fpi_old").fillna(0) + fvci)
        dom_inst = np.where(has_new, g(src, "inst_domestic_new") - g(src, "nbfc").fillna(0),
                            g(src, "inst_old") - g(src, "fpi_old").fillna(0) - fvci)
        w[f"promoter_{name}"] = g(src, "promoter")
        w[f"public_{name}"] = g(src, "public")
        w[f"mf_{name}"] = g(src, "mf")
        w[f"insurance_{name}"] = g(src, "insurance")
        w[f"banks_{name}"] = g(src, "banks")
        w[f"pension_{name}"] = g(src, "pension")
        w[f"aif_{name}"] = g(src, "aif")
        w[f"fpi_{name}"] = fpi
        w[f"fii_{name}"] = fii
        w[f"foreign_inst_{name}"] = foreign_inst
        w[f"dom_inst_{name}"] = dom_inst
        w[f"inst_{name}"] = pd.Series(foreign_inst, index=src.index) + pd.Series(dom_inst, index=src.index)
        w[f"fdi_public_{name}"] = g(src, "fdi_public")
        w[f"inst_reported_{name}"] = np.where(has_new, g(src, "inst_foreign_new") + g(src, "inst_domestic_new"), g(src, "inst_old"))
        w[f"noninst_{name}"] = g(src, "noninst")
    w = w.reset_index()
    sp = fil.merge(w, on="filename", how="inner")
    sp["is_quarter_end"] = sp.period_end.dt.is_quarter_end

    # one record per (symbol, period_end): earliest-available filing wins for PIT purposes
    sp = sp.sort_values(["symbol", "period_end", "avail_date", "revised"])
    sp = sp.drop_duplicates(["symbol", "period_end"], keep="first").reset_index(drop=True)

    # ---- quarter-on-quarter changes (quarter-end filings only) ----------------------------
    q = sp[sp.is_quarter_end].sort_values(["symbol", "period_end"]).copy()
    q["prev_period_end"] = q.groupby("symbol").period_end.shift(1)
    gap_q = ((q.period_end - q.prev_period_end).dt.days / 91.3).round()
    q["gap_quarters"] = gap_q
    split_adj = _split_factor(q, prices) if prices is not None else pd.Series(1.0, index=q.index)
    q["split_factor"] = split_adj
    prev_total = q.groupby("symbol").total_shares.shift(1) * q.split_factor
    q["share_change_ex_split"] = q.total_shares / prev_total - 1
    q["dilution_flag"] = q.share_change_ex_split.abs() > 0.01
    if prices is not None:
        first_trade = prices.groupby("symbol").date.min()
        q["listing_date"] = q.symbol.map(first_trade)
        q["ipo_quarter"] = q.listing_date > q.prev_period_end
    else:
        q["ipo_quarter"] = False
    for c in ("promoter", "mf", "insurance", "fpi", "fii", "dom_inst", "inst", "foreign_inst", "public", "noninst"):
        q[f"d_{c}_1q"] = q[f"{c}_pct"] - q.groupby("symbol")[f"{c}_pct"].shift(1)
        q[f"d_{c}_2q"] = q[f"{c}_pct"] - q.groupby("symbol")[f"{c}_pct"].shift(2)
        q[f"d_{c}_4q"] = q[f"{c}_pct"] - q.groupby("symbol")[f"{c}_pct"].shift(4)
        prev_sh = q.groupby("symbol")[f"{c}_sh"].shift(1) * q.split_factor
        # net shares bought (split-adjusted) as % of current shares outstanding
        q[f"flow_{c}_1q"] = (q[f"{c}_sh"] - prev_sh) / q.total_shares * 100
    invalid = (q.gap_quarters != 1) | q.ipo_quarter
    for c in [x for x in q.columns if x.startswith(("d_", "flow_")) and x.endswith("_1q")]:
        q.loc[invalid, c] = np.nan
    # consecutive quarters of institutional accumulation
    q["inst_acc_streak"] = _streak(q, "d_inst_1q")
    q["mf_acc_streak"] = _streak(q, "d_mf_1q")
    q["fii_acc_streak"] = _streak(q, "d_fii_1q")
    q["free_float_pct"] = 100 - q.promoter_pct
    q["d_inst_1q_ff"] = q.d_inst_1q / q.free_float_pct.clip(lower=5) * 100

    out = ensure_dir(proc)
    sp.to_parquet(out / "shareholding_filings_pit.parquet", index=False)
    q.to_parquet(out / "shareholding_quarterly.parquet", index=False)
    register_dataset(
        "shareholding_quarterly", source_url="derived from shp_xbrl",
        coverage_start=str(q.period_end.min().date()), coverage_end=str(q.period_end.max().date()),
        n_records=int(len(q)), n_symbols=int(q.symbol.nunique()),
        missingness=f"d_inst_1q missing {q.d_inst_1q.isna().mean():.3f} (first filing, gaps, IPO quarters)",
        quality_score=0.85,
        limitations="Category totals per SEBI SHP format; FII = FPI cat I+II (+ foreign SWF in 2022+ taxonomy).",
        path="data/processed/shareholding_quarterly.parquet")
    log.info("shareholding: %d filings, %d quarter rows, %d symbols", len(sp), len(q), q.symbol.nunique())
    return q


def _streak(q: pd.DataFrame, col: str) -> pd.Series:
    acc = (q[col] > 0).astype(int)
    grp = (acc != acc.groupby(q.symbol).shift()).cumsum()
    return acc.groupby([q.symbol, grp]).cumsum() * acc


def _split_factor(q: pd.DataFrame, prices: pd.DataFrame) -> pd.Series:
    """Product of NSE corporate-action ratios (split/bonus/rights rebasing) between consecutive
    period ends, used to restate the previous quarter's share counts."""
    ca = prices.loc[prices.ca_flag, ["symbol", "date", "ca_ratio"]]
    out = pd.Series(1.0, index=q.index)
    if ca.empty:
        return out
    ca_by = {s: g for s, g in ca.groupby("symbol")}
    for i, (s, pe, ppe) in enumerate(zip(q.symbol, q.period_end, q.prev_period_end)):
        g = ca_by.get(s)
        if g is None or pd.isna(ppe):
            continue
        m = (g.date > ppe) & (g.date <= pe)
        if m.any():
            out.iloc[i] = float(np.prod(g.loc[m, "ca_ratio"].values))
    return out


# ----------------------------------------------------------------------------- named holders
def build_named_holders(sp: pd.DataFrame) -> pd.DataFrame:
    """Quarter-on-quarter changes of named (>1%) institutional holders, aggregated by family."""
    nh = pd.read_parquet(path("processed") / "shp_named_holders.parquet")
    nh = nh[nh.name.notna() & (nh.name.str.len() > 2)]
    nh = nh[~nh.name.str.upper().isin(["OTHER", "OTHERS", "HUF", "TRUSTS", "CLEARING MEMBER", "CLEARING MEMBERS",
                                       "BODIES CORPORATE", "IEPF", "LLP", "NRI", "NON-RESIDENT INDIAN (NRI)"])]
    nh = classify_frame(nh, "name", "axis")
    keys = sp[["filename", "symbol", "period_end", "effective_date", "is_quarter_end", "total_shares"]]
    nh = nh.merge(keys, on="filename", how="inner")
    # "persons acting in concert" rows duplicate holders listed elsewhere -> drop
    nh = nh[~nh.axis.str.contains("PersonsInConcert", na=False)]
    fam = (nh.groupby(["symbol", "period_end", "family", "category"], as_index=False)
             .agg(pct=("pct", "sum"), shares=("shares", "sum"), n_entities=("name", "nunique")))
    fam.to_parquet(path("processed") / "named_holder_families.parquet", index=False)

    # investor classification database (observed names)
    db = (nh.groupby(["name", "family", "category", "confidence"], dropna=False)
            .agg(shp_axis=("axis", "first"), notes=("notes", "first"), n_filings=("filename", "nunique"),
                 first_seen=("period_end", "min"), last_seen=("period_end", "max"))
            .reset_index().sort_values("n_filings", ascending=False))
    db = db.rename(columns={"name": "original_name", "family": "normalised_name", "category": "investor_category",
                            "confidence": "classification_confidence"})
    db["source"] = "NSE SHP XBRL named >1% holders; rule-based classifier v1"
    db.to_csv(path("processed") / "investor_names_observed.csv", index=False)
    from src.common import ROOT
    from src.investor_classification.classifier import write_classification_db
    write_classification_db(db, ROOT / "config" / "investor_classification.csv")
    return fam


def named_holder_flows(fam: pd.DataFrame, q: pd.DataFrame) -> pd.DataFrame:
    """Per (symbol, quarter): credible-buyer entries/increases, large-holder exits, absorption."""
    # promoter-group entities are netted as one holder (intra-group transfers are not exits)
    fam = fam.copy()
    fam.loc[fam.category == "promoter_group", "family"] = "PROMOTER GROUP"
    # one row per (symbol, quarter, family); category = that of the family's largest line
    fam = (fam.sort_values("pct", ascending=False)
              .groupby(["symbol", "period_end", "family"], as_index=False)
              .agg(category=("category", "first"), pct=("pct", "sum")))
    qe = q[["symbol", "period_end", "prev_period_end", "gap_quarters", "ipo_quarter", "d_inst_1q",
            "d_promoter_1q"]]
    cur = fam.merge(qe[["symbol", "period_end"]], on=["symbol", "period_end"], how="inner")
    prev = fam.rename(columns={"period_end": "prev_period_end", "pct": "pct_prev", "category": "category_prev"})
    prev = prev.merge(qe[["symbol", "prev_period_end", "period_end"]].dropna(), on=["symbol", "prev_period_end"], how="inner")
    m = cur.merge(prev[["symbol", "period_end", "family", "pct_prev", "category_prev"]],
                  on=["symbol", "period_end", "family"], how="outer")
    m["category"] = m.category.fillna(m.category_prev).fillna("unknown")
    m["pct"] = m.pct.fillna(0.0)
    m["pct_prev"] = m.pct_prev.fillna(0.0)  # below 1% threshold => treated as 0 (documented)
    m["dpct"] = m.pct - m.pct_prev
    m["credible"] = m.category.isin(CREDIBLE_CATEGORIES)
    m["quality"] = m.category.map(CATEGORY_QUALITY).fillna(0.2)
    m["is_seller_type"] = m.category.isin(["pe_vc_strategic", "promoter_group", "unknown"])

    def agg(g: pd.DataFrame) -> pd.Series:
        buys = g[(g.dpct > 0)]
        cb = buys[buys.credible]
        exits = g[(g.dpct <= -1.0) & g.is_seller_type]
        tot_exit = exits.dpct.abs().sum()
        return pd.Series({
            "nh_credible_new": int(((g.pct_prev == 0) & (g.pct > 0) & g.credible).sum()),
            "nh_credible_increase_pp": cb.dpct.sum(),
            "nh_credible_decrease_pp": g[(g.dpct < 0) & g.credible].dpct.abs().sum(),
            "nh_n_buyer_families": int(cb.family.nunique()),
            "nh_n_credible_holders": int(((g.pct > 0) & g.credible).sum()),
            "nh_buyer_quality": float((buys.dpct * buys.quality).sum() / buys.dpct.sum()) if buys.dpct.sum() > 0 else np.nan,
            "nh_short_horizon_buy_pp": buys[buys.category.isin(["arbitrage_desk", "proprietary", "broker_dealer"])].dpct.sum(),
            "nh_passive_buy_pp": buys[buys.category == "etf_passive"].dpct.sum(),
            "nh_foreign_lo_buy_pp": cb[cb.category.isin(["foreign_long_only", "foreign_mutual_fund", "sovereign_wealth", "foreign_pension"])].dpct.sum(),
            "nh_domestic_mf_buy_pp": cb[cb.category == "domestic_mf"].dpct.sum(),
            "nh_large_exit_pp": tot_exit,
            "nh_seller_concentration": float(exits.dpct.abs().max() / tot_exit) if tot_exit > 0 else np.nan,
            "nh_top_seller": exits.loc[exits.dpct.idxmin(), "family"] if len(exits) else None,
        })

    res = m.groupby(["symbol", "period_end"]).apply(agg, include_groups=False).reset_index()
    res = res.merge(qe, on=["symbol", "period_end"], how="left")
    invalid = (res.gap_quarters != 1) | res.ipo_quarter.fillna(False)
    num = [c for c in res.columns if c.startswith("nh_") and c not in ("nh_top_seller", "nh_n_credible_holders")]
    res.loc[invalid, num] = np.nan
    # absorption: a large holder (PE/strategic/promoter/unknown) exited >=2pp and institutions
    # (category totals) absorbed at least half of it in the same quarter
    res["absorption_event"] = (res.nh_large_exit_pp >= 2.0) & (res.d_inst_1q >= 0.5 * res.nh_large_exit_pp)
    res["absorption_ratio"] = np.where(res.nh_large_exit_pp >= 1.0, res.d_inst_1q / res.nh_large_exit_pp, np.nan)
    res.to_parquet(path("processed") / "named_holder_flows.parquet", index=False)
    return res
