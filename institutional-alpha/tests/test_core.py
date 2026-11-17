"""Unit tests for point-in-time logic, parsing, classification and portfolio mechanics."""
from __future__ import annotations

import gzip
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.backtesting.engine import one_way_cost
from src.data_acquisition.shp_xbrl import parse_xbrl_text
from src.data_cleaning.prices import canonicalize, parse_dividend
from src.data_cleaning.shareholding import next_session
from src.investor_classification.classifier import classify
from src.portfolio.construction import cap_weights, select

FIX = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- investor classification
@pytest.mark.parametrize("name,expected", [
    ("NORGES BANK ON ACCOUNT OF THE GOVERNMENT PENSION FUND GLOBAL", "sovereign_wealth"),
    ("Government Of Singapore", "sovereign_wealth"),
    ("FIDELITY INVESTMENT TRUST FIDELITY EMERGING ASIA FUND", "foreign_mutual_fund"),
    ("SMALLCAP WORLD FUND, INC", "foreign_mutual_fund"),
    ("NIPPON LIFE INDIA TRUSTEE LTD-A/C NIPPON INDIA PHARMA FUND", "domestic_mf"),
    ("GRAVITON RESEARCH CAPITAL LLP", "proprietary"),
    ("ALPHAGREP SECURITIES PRIVATE LIMITED", "proprietary"),
    ("GOLDMAN SACHS FUNDS - GOLDMAN SACHS INDIA EQUITY PORTFOLIO", "foreign_mutual_fund"),
    ("GOLDMAN SACHS (SINGAPORE) PTE. - ODI", "arbitrage_desk"),
    ("SBI BSE SENSEX ETF", "etf_passive"),
    ("KOTAK EQUITY ARBITRAGE FUND", "arbitrage_desk"),
    ("ICICI PRUDENTIAL LIFE INSURANCE COMPANY LIMITED", "insurance"),
    ("TPG ASIA VII SF PTE LTD", "pe_vc_strategic"),
])
def test_classifier_examples(name, expected):
    assert classify(name)["category"] == expected


def test_fpi_never_defaults_to_long_only():
    r = classify("XYZ EMERGING OPPORTUNITIES MASTER FUND", "DetailsOfSharesHeldByInstitutionsForeignPortfolioInvestorOneAxis")
    assert r["category"] == "unknown" and r["confidence"] == "low"


def test_family_grouping_counts_one_buyer():
    a = classify("FIDELITY FUNDS - ASIAN SPECIAL SITUATIONS FUND")["family"]
    b = classify("FIDELITY INVESTMENT TRUST : FIDELITY EMERGING MARKETS FUND")["family"]
    assert a == b == "FIDELITY"


# ---------------------------------------------------------------- dividends / corporate actions
@pytest.mark.parametrize("purpose,dps", [
    ("AGM/DIV - RS 10 PER SH/", 10.0),
    ("DIV-RS19/SPLDIV-RS3PERSH", 22.0),
    ("INTERIM DIVIDEND - RS 2.50 PER SHARE", 2.5),
    ("FV SPLT FRM RS 6 TO RE 1", 0.0),
    ("BONUS 1:1", 0.0),
    ("ANNUAL GENERAL MEETING", 0.0),
])
def test_parse_dividend(purpose, dps):
    assert parse_dividend(purpose) == pytest.approx(dps)


def test_symbol_chain_respects_effective_date():
    sc = pd.DataFrame({"old": ["ZOMATO"], "new": ["ETERNAL"], "eff": [pd.Timestamp("2025-04-09")]})
    df = pd.DataFrame({"symbol": ["ZOMATO", "ZOMATO", "ETERNAL"],
                       "date": pd.to_datetime(["2024-07-16", "2025-04-08", "2025-04-09"])})
    out = canonicalize(df, sc)
    assert list(out.symbol) == ["ETERNAL", "ETERNAL", "ETERNAL"]
    assert list(out.symbol_raw) == ["ZOMATO", "ZOMATO", "ETERNAL"]


# ---------------------------------------------------------------- point-in-time timing
def test_next_session_is_strictly_after_broadcast():
    cal = pd.DatetimeIndex(pd.to_datetime(["2025-07-17", "2025-07-18", "2025-07-21", "2025-07-22"]))
    s = pd.Series(pd.to_datetime(["2025-07-18", "2025-07-19", "2025-07-17"]))
    out = next_session(s, cal)
    assert list(out) == list(pd.to_datetime(["2025-07-21", "2025-07-21", "2025-07-18"]))


def test_no_signal_uses_future_filing():
    """Panel invariant: every shareholding signal's availability date precedes its signal date."""
    from src.common import path
    p = path("processed") / "monthly_panel.parquet"
    if not p.exists():
        pytest.skip("panel not built")
    panel = pd.read_parquet(p, columns=["date", "sig_avail_date", "exec_date", "bm_date"])
    ok = panel.sig_avail_date.isna() | (panel.sig_avail_date < panel.date)
    assert ok.all()
    assert (panel.exec_date.isna() | (panel.exec_date > panel.date)).all()
    # only the latest (live) signal date may lack an execution session
    assert panel.loc[panel.exec_date.isna(), "date"].nunique() <= 1


# ---------------------------------------------------------------- SHP XBRL parsing
def test_shp_parser_fixture():
    txt = gzip.open(FIX / "SHP_197579_1416643_17042025114547_WEB.xml.gz", "rt").read()
    r = parse_xbrl_text(txt)
    cats = {c["member"]: c["pct"] for c in r["categories"]}
    assert r["xbrl_symbol"] == "SAILIFE"
    assert cats["ShareholdingPatternMember"] == pytest.approx(100.0)
    assert cats["ShareholdingOfPromoterAndPromoterGroupMember"] + cats["PublicShareholdingMember"] == pytest.approx(100.0, abs=0.1)
    names = {h["name"] for h in r["holders"]}
    assert "SMALLCAP WORLD FUND, INC" in names
    scwf = [h for h in r["holders"] if h["name"] == "SMALLCAP WORLD FUND, INC"][0]
    assert scwf["pct"] == pytest.approx(2.62, abs=0.01)


# ---------------------------------------------------------------- portfolio mechanics
def test_cap_weights_respects_cap():
    w = cap_weights(pd.Series([10, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], dtype=float), 0.05)
    assert w.max() <= 0.05 + 1e-12
    assert w.sum() == pytest.approx(1.0, abs=1e-6)


def test_select_top_n_equal_weight():
    c = pd.DataFrame({"symbol": list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "s": np.arange(26.0), "adtv_63_cr": 5.0})
    w = select(c, "s", 20, "equal", 0.05)
    assert len(w) == 20 and w.max() == pytest.approx(0.05) and "Z" in w.index and "A" not in w.index


def test_costs_small_cap_more_expensive_and_buy_includes_stamp():
    assert one_way_cost("small", "buy") > one_way_cost("large", "buy")
    assert one_way_cost("large", "buy") - one_way_cost("large", "sell") == pytest.approx(1.5e-4)


@pytest.mark.parametrize("purpose,factor", [
    ("FV SPLT FRM RS 10 TO RS 2", 5.0),
    ("FVSPLT FRM RS 5 TO RE 1", 5.0),
    ("BONUS 1:2", 1.5),
    ("BONUS 1:1", 2.0),
    ("CNSLDATNRE1 TO RS10", 0.1),
    ("DIV - RS 5 PER SH", 1.0),
])
def test_share_factor(purpose, factor):
    from src.data_cleaning.prices import parse_share_factor
    assert parse_share_factor(purpose) == pytest.approx(factor)


def test_shp_parser_new_taxonomy_named_holders():
    """V1.1 taxonomy pairs 'D_<axis>_ContextN' (name) with '<axis>_ContextN' (numbers)."""
    txt = gzip.open(FIX / "SHP_1486390_18072025024243_WEB.xml.gz", "rt").read()
    r = parse_xbrl_text(txt)
    tpg = [h for h in r["holders"] if "TPG" in h["name"].upper()]
    assert tpg and tpg[0]["pct"] == pytest.approx(14.72, abs=0.01)
    assert sum(h["pct"] is None for h in r["holders"]) == 0
