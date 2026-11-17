"""Rule-based investor classification.

Every institutional name we observe (SHP named >1% holders; bulk/block-deal client names) is mapped
to (family, category, confidence, notes). `family` is the economic decision-maker used to count
*independent* institutions (e.g. 30 Fidelity feeder funds = one buyer family).

Categories (the 13 requested + one added because large-holder exits are central to the thesis):
  foreign_long_only      foreign active asset managers / institutional accounts
  sovereign_wealth       sovereign wealth funds & central banks (GIC, Norges Bank, ADIA, KIA, Temasek)
  foreign_pension        foreign public/private pension plans
  foreign_mutual_fund    foreign retail fund vehicles of active managers (SICAV/OEIC/40-Act)
  domestic_mf            Indian mutual funds (active schemes)
  insurance              insurance companies
  bank                   banks / financial institutions investing own book
  etf_passive            ETFs, index funds, passive / factor vehicles
  proprietary            proprietary / HFT trading firms
  arbitrage_desk         ODI issuers, arbitrage schemes, derivative hedging desks
  broker_dealer          broker-dealer own accounts
  promoter_group         promoter & promoter-group entities
  unknown                not classifiable with the rules (never assumed long-only)
  pe_vc_strategic        (added) private equity, venture capital, strategic/corporate holders

Confidence: high = curated named-entity rule; medium = keyword rule; low = SHP-axis fallback only.
Rules are ordered: the first match wins, so specific entity rules precede generic keywords.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

LONG_ONLY_CATEGORIES = {"foreign_long_only", "sovereign_wealth", "foreign_pension", "foreign_mutual_fund",
                        "domestic_mf", "insurance"}
CREDIBLE_CATEGORIES = LONG_ONLY_CATEGORIES  # "credible institutional" buyer set used in scoring
SHORT_HORIZON_CATEGORIES = {"proprietary", "arbitrage_desk", "broker_dealer"}

# quality weights used by the buyer-quality score (documented in scoring_weights.yaml)
CATEGORY_QUALITY = {
    "sovereign_wealth": 1.0, "foreign_pension": 1.0, "foreign_long_only": 0.9, "foreign_mutual_fund": 0.9,
    "domestic_mf": 0.85, "insurance": 0.7, "bank": 0.4, "etf_passive": 0.2, "pe_vc_strategic": 0.3,
    "unknown": 0.2, "broker_dealer": 0.05, "arbitrage_desk": 0.0, "proprietary": 0.0, "promoter_group": 0.0,
}


@dataclass(frozen=True)
class Rule:
    pattern: str
    family: str | None
    category: str
    confidence: str
    notes: str = ""


# (pattern, family, category, confidence, notes) — family None => derive from cleaned name
R = Rule
RULES: list[Rule] = [
    # ---- arbitrage / ODI / derivative desks (before bank & broker names) -------------------
    R(r"\bODI\b|OFFSHORE DERIVATIVE|PARTICIPATORY NOTE", None, "arbitrage_desk", "high", "ODI/P-note issuer account"),
    R(r"SOCIETE GENERALE", "SOCIETE GENERALE", "arbitrage_desk", "medium",
      "SocGen India FPI accounts are predominantly ODI/derivative-hedge books"),
    R(r"BNP PARIBAS ARBITRAGE", "BNP PARIBAS", "arbitrage_desk", "high", ""),
    R(r"COPTHALL MAURITIUS", "JPMORGAN (ODI)", "arbitrage_desk", "high", "J.P. Morgan ODI vehicle"),
    R(r"CITIGROUP GLOBAL MARKETS MAURITIUS", "CITIGROUP", "arbitrage_desk", "high", "Citi ODI/derivative vehicle"),
    R(r"MERRILL LYNCH MARKETS SINGAPORE|BOFA SECURITIES EUROPE", "BANK OF AMERICA", "arbitrage_desk", "high", ""),
    R(r"MORGAN STANLEY ASIA \(?SINGAPORE\)?", "MORGAN STANLEY", "arbitrage_desk", "high", "MS Asia (Singapore) = ODI/hedge book"),
    R(r"GOLDMAN SACHS \(?SINGAPORE\)? PTE|GOLDMAN SACHS INTERNATIONAL", "GOLDMAN SACHS", "arbitrage_desk", "medium",
      "GS dealer entity; not the GS Funds long-only range"),
    R(r"BARCLAYS (MERCHANT BANK|BANK PLC|CAPITAL)", "BARCLAYS", "arbitrage_desk", "medium", ""),
    R(r"UBS AG|UBS PRINCIPAL CAPITAL|UBS SECURITIES", "UBS", "arbitrage_desk", "medium", ""),
    R(r"CREDIT SUISSE \(?SINGAPORE|CREDIT SUISSE SECURITIES", "CREDIT SUISSE", "arbitrage_desk", "medium", ""),
    R(r"NOMURA SINGAPORE|NOMURA FINANCIAL PRODUCTS", "NOMURA", "arbitrage_desk", "medium", ""),
    R(r"MACQUARIE BANK", "MACQUARIE", "arbitrage_desk", "medium", ""),
    R(r"\bARBITRAGE\b", None, "arbitrage_desk", "high", "arbitrage scheme"),
    # ---- proprietary / HFT ------------------------------------------------------------------
    R(r"GRAVITON", "GRAVITON RESEARCH CAPITAL", "proprietary", "high", ""),
    R(r"ALPHAGREP|ALPHA GREP", "ALPHAGREP", "proprietary", "high", ""),
    R(r"TOWER RESEARCH", "TOWER RESEARCH", "proprietary", "high", ""),
    R(r"JANE STREET", "JANE STREET", "proprietary", "high", ""),
    R(r"OPTIVER|CITADEL SECURITIES|VIRTU|SUSQUEHANNA|IMC TRADING|HUDSON RIVER", None, "proprietary", "high", ""),
    R(r"\bHRTI\b|JUNOMONETA|QE SECURITIES|NK SECURITIES RESEARCH|IRAGE|MICROCURVES|SILVERLEAF|PACE STOCK|"
      r"MUSIGMA|DOLAT|ELIXIR|SAINATH|MANSI SHARE|GIRDHAR|SHAREKHAN SECURITIES TRADING", None, "proprietary", "medium",
      "frequent intraday/HFT bulk-deal counterparties"),
    # ---- passive ----------------------------------------------------------------------------
    R(r"\bETF\b|EXCHANGE TRADED|\bBEES\b|ISHARES|SPDR|XTRACKERS|VANECK|WISDOMTREE|INDEX FUND|\bINDEX\b|"
      r"TRACKER|PASSIVE|\bMSCI\b|\bFTSE\b|NIFTY ?50|NIFTY ?NEXT|SENSEX|EMERGING MARKETS STOCK INDEX|"
      r"TOTAL INTERNATIONAL STOCK|DIMENSIONAL|FIRST TRUST", None, "etf_passive", "medium", "index/ETF vehicle"),
    R(r"VANGUARD", "VANGUARD", "etf_passive", "medium", "Vanguard India exposure is predominantly index funds"),
    R(r"STATE STREET", "STATE STREET", "etf_passive", "low", ""),
    # ---- sovereign wealth / central banks ---------------------------------------------------
    R(r"GOVERNMENT OF SINGAPORE|\bGIC\b", "GIC (SINGAPORE)", "sovereign_wealth", "high", ""),
    R(r"MONETARY AUTHORITY OF SINGAPORE", "MAS (SINGAPORE)", "sovereign_wealth", "high", ""),
    R(r"NORGES BANK|GOVERNMENT PENSION FUND GLOBAL", "NORGES BANK", "sovereign_wealth", "high",
      "Norges Bank Investment Management (GPFG)"),
    R(r"ABU DHABI INVESTMENT AUTHORITY|\bADIA\b|PLATINUM JASMINE|PLATINUM OWL", "ADIA", "sovereign_wealth", "high", ""),
    R(r"KUWAIT INVESTMENT AUTHORITY", "KUWAIT INVESTMENT AUTHORITY", "sovereign_wealth", "high", ""),
    R(r"QATAR INVESTMENT AUTHORITY|QIA\b", "QATAR INVESTMENT AUTHORITY", "sovereign_wealth", "high", ""),
    R(r"PUBLIC INVESTMENT FUND", "PIF (SAUDI)", "sovereign_wealth", "high", ""),
    R(r"TEMASEK|CAMAS INVESTMENTS|DUNEARN INVESTMENTS|ALTI ?INVESTMENTS|ANDERSON INVESTMENTS", "TEMASEK",
      "sovereign_wealth", "high", "Temasek holding vehicles"),
    R(r"MUBADALA|ABU DHABI|EMIRATES INVESTMENT", None, "sovereign_wealth", "medium", ""),
    R(r"CENTRAL BANK|SAUDI ARABIAN MONETARY|BANK NEGARA|SAFE INVESTMENT", None, "sovereign_wealth", "medium", ""),
    R(r"CHINA INVESTMENT CORP|KOREA INVESTMENT CORP|KHAZANAH|FUTURE FUND BOARD", None, "sovereign_wealth", "high", ""),
    # ---- foreign pensions --------------------------------------------------------------------
    R(r"CANADA PENSION PLAN|CPP INVESTMENT", "CPP INVESTMENTS", "foreign_pension", "high", ""),
    R(r"ONTARIO TEACHERS|OMERS|CAISSE DE DEPOT|CDPQ|BRITISH COLUMBIA INVESTMENT|PUBLIC SECTOR PENSION|"
      r"PSP INVESTMENTS|ALBERTA INVESTMENT", None, "foreign_pension", "high", "Canadian pension"),
    R(r"CALIFORNIA PUBLIC EMPLOYEES|CALPERS|CALIFORNIA STATE TEACHERS|NEW YORK STATE COMMON|FLORIDA RETIREMENT|"
      r"TEACHER RETIREMENT SYSTEM|PUBLIC EMPLOYEES RETIREMENT|STATE OF WISCONSIN|EMPLOYEES RETIREMENT|"
      r"PENSION|PENSIOEN|SUPERANNUATION|AUSTRALIANSUPER|BUREAU OF LABOR FUNDS|NATIONAL PENSION|"
      r"RETIREMENT SYSTEM|\bAP[1-7]\b|FORSTA AP|ANDRA AP|STICHTING", None, "foreign_pension", "medium", ""),
    # ---- domestic mutual funds (after ETF/arbitrage rules) -----------------------------------
    R(r"NIPPON (LIFE )?INDIA|RELIANCE CAPITAL TRUSTEE|RELIANCE MUTUAL", "NIPPON INDIA MF", "domestic_mf", "high", ""),
    R(r"HDFC (TRUSTEE|MUTUAL|.*FUND)", "HDFC MF", "domestic_mf", "high", ""),
    R(r"\bSBI\b.*(FUND|MUTUAL|SCHEME)", "SBI MF", "domestic_mf", "high", ""),
    R(r"ICICI PRUDENTIAL(?!.*LIFE).*(FUND|MUTUAL|SCHEME)", "ICICI PRUDENTIAL MF", "domestic_mf", "high", ""),
    R(r"AXIS (MUTUAL|.*FUND)", "AXIS MF", "domestic_mf", "high", ""),
    R(r"KOTAK (MAHINDRA TRUSTEE|.*FUND)(?!.*INTERNATIONAL)", "KOTAK MF", "domestic_mf", "high", ""),
    R(r"ADITYA BIRLA SUN LIFE (TRUSTEE|.*FUND)|BIRLA SUN LIFE", "ADITYA BIRLA SUN LIFE MF", "domestic_mf", "high", ""),
    R(r"\bUTI\b.*(FUND|MUTUAL|SCHEME)|UTI-", "UTI MF", "domestic_mf", "high", ""),
    R(r"MIRAE ASSET.*(FUND|MUTUAL)", "MIRAE ASSET MF", "domestic_mf", "high", ""),
    R(r"\bDSP\b|DSP BLACKROCK", "DSP MF", "domestic_mf", "high", ""),
    R(r"FRANKLIN (TEMPLETON )?INDIA|TEMPLETON INDIA", "FRANKLIN TEMPLETON INDIA MF", "domestic_mf", "high", ""),
    R(r"INVESCO INDIA|INVESCO TRUSTEE|INEVSCO INDIA", "INVESCO INDIA MF", "domestic_mf", "high", "incl. filing typo INEVSCO"),
    R(r"HSBC (MUTUAL|.*FUND)(?!.*(GLOBAL|INDIAN EQUITY))|L&T MUTUAL|L ?& ?T .*FUND", "HSBC MF (incl. L&T)", "domestic_mf", "high", ""),
    R(r"MOTILAL OSWAL.*(FUND|MUTUAL|TRUSTEE)", "MOTILAL OSWAL MF", "domestic_mf", "high", ""),
    R(r"TATA (MUTUAL|.*FUND)(?!.*AIA)", "TATA MF", "domestic_mf", "high", ""),
    R(r"CANARA ROBECO", "CANARA ROBECO MF", "domestic_mf", "high", ""),
    R(r"\bQUANT\b.*(FUND|MUTUAL)", "QUANT MF", "domestic_mf", "high", ""),
    R(r"PARAG PARIKH|PPFAS", "PPFAS MF", "domestic_mf", "high", ""),
    R(r"EDELWEISS.*(FUND|MUTUAL|TRUSTEE)", "EDELWEISS MF", "domestic_mf", "high", ""),
    R(r"SUNDARAM.*(FUND|MUTUAL|TRUSTEE)", "SUNDARAM MF", "domestic_mf", "high", ""),
    R(r"BANDHAN.*(FUND|MUTUAL)|IDFC.*(FUND|MUTUAL)", "BANDHAN MF (ex-IDFC)", "domestic_mf", "high", ""),
    R(r"BARODA (BNP|PIONEER)|BNP PARIBAS MUTUAL", "BARODA BNP PARIBAS MF", "domestic_mf", "high", ""),
    R(r"WHITEOAK CAPITAL.*(FUND|MUTUAL)", "WHITEOAK MF", "domestic_mf", "high", ""),
    R(r"360 ONE.*FUND|IIFL MUTUAL", "360 ONE MF", "domestic_mf", "high", ""),
    R(r"UNION (MUTUAL|.*FUND)|MAHINDRA MANULIFE|PGIM INDIA|\bITI\b.*FUND|JM FINANCIAL.*FUND|LIC MUTUAL|"
      r"LIC MF|NAVI MUTUAL|GROWW MUTUAL|HELIOS|TRUST MUTUAL|SAMCO MUTUAL|BAJAJ FINSERV MUTUAL|OLD BRIDGE",
      None, "domestic_mf", "high", ""),
    R(r"MUTUAL FUND|\bMF\b|TRUSTEE (CO|COMPANY|LTD|LIMITED|PVT)", None, "domestic_mf", "medium", "generic Indian MF trustee"),
    # ---- insurance ---------------------------------------------------------------------------
    R(r"LIFE INSURANCE CORPORATION|\bLIC\b", "LIC OF INDIA", "insurance", "high", ""),
    R(r"GENERAL INSURANCE CORPORATION|NEW INDIA ASSURANCE|UNITED INDIA INSURANCE|NATIONAL INSURANCE|"
      r"ORIENTAL INSURANCE", None, "insurance", "high", "public-sector insurer"),
    R(r"INSURANCE|ASSURANCE|LIFE CO|SBI LIFE|HDFC LIFE|HDFC STANDARD LIFE|PRUDENTIAL LIFE|MAX LIFE|BAJAJ ALLIANZ|"
      r"TATA AIA|KOTAK MAHINDRA LIFE|AVIVA|RELIANCE NIPPON LIFE|BHARTI AXA|PNB METLIFE|CANARA HSBC|LOMBARD|"
      r"STAR HEALTH", None, "insurance", "medium", ""),
    # ---- foreign long-only asset managers / funds -----------------------------------------------
    R(r"SMALL ?CAP WORLD FUND|SMALLCAP WORLD|EUROPACIFIC GROWTH|NEW WORLD FUND|NEW PERSPECTIVE FUND|"
      r"CAPITAL GROUP|CAPITAL INTERNATIONAL|AMERICAN FUNDS|AMCAP|GROWTH FUND OF AMERICA|FUNDAMENTAL INVESTORS",
      "CAPITAL GROUP", "foreign_mutual_fund", "high", "Capital Group (American Funds) active range"),
    R(r"FIDELITY|FIAM\b|FIL INVESTMENT", "FIDELITY", "foreign_mutual_fund", "high", "Fidelity (FMR/FIL) active funds"),
    R(r"T\.? ?ROWE PRICE", "T. ROWE PRICE", "foreign_mutual_fund", "high", ""),
    R(r"GOLDMAN SACHS (FUNDS|INDIA EQUITY|ASSET)", "GOLDMAN SACHS ASSET MANAGEMENT", "foreign_mutual_fund", "high",
      "GS Funds SICAV India Equity Portfolio (long-only)"),
    R(r"MORGAN STANLEY (INVESTMENT FUNDS|INSTITUTIONAL FUND|ASIA OPPORTUNITY|INDIA INVESTMENT)", "MORGAN STANLEY IM",
      "foreign_mutual_fund", "high", "MSIM long-only"),
    R(r"MATTHEWS|ABERDEEN|ABRDN|STEWART INVESTORS|FIRST STATE|NOMURA (INDIA|FUNDS)|JPMORGAN (FUNDS|INDIA)|"
      r"JP MORGAN (FUNDS|INDIA)|SCHRODER|BLACKROCK (GLOBAL FUNDS|INDIA)|INVESCO (FUNDS|ASIA|EMERGING)|"
      r"FRANKLIN TEMPLETON INVESTMENT FUNDS|TEMPLETON (DEVELOPING|EMERGING|ASIAN)|ASHMORE|HSBC GLOBAL INVESTMENT|"
      r"HSBC INDIAN EQUITY|NINETY ONE|ROBECO|AMUNDI|PICTET|COMGEST|CARMIGNAC|ALLIANZ GLOBAL INVESTORS|"
      r"POLAR CAPITAL|NEUBERGER|WASATCH|BARON|ARTISAN|DRIEHAUS|WILLIAM BLAIR|OPPENHEIMER|HARDING LOEVNER|"
      r"LAZARD|GQG|WELLINGTON|COLUMBIA|THREADNEEDLE|JANUS|HENDERSON|MANULIFE|EASTSPRING|UBS (ASSET|\(LUX\))|"
      r"KOTAK (FUNDS|MAHINDRA \(INTERNATIONAL\))|ASHOKA|STEADVIEW|WHITEOAK CAPITAL (MAURITIUS|FUND|PARTNERS)|"
      r"AMANSA|NALANDA|GOLDMAN SACHS TRUST|VONTOBEL|MIRAE ASSET GLOBAL|ELARA|GOVERNMENT EMPLOYEES",
      None, "foreign_mutual_fund", "medium", "known active foreign manager"),
    R(r"BLACKROCK|BLACK ROCK", "BLACKROCK", "foreign_long_only", "low",
      "unspecified BlackRock entity: active (BGF) or passive (iShares) cannot be told apart"),
    R(r"MORGAN STANLEY", "MORGAN STANLEY", "broker_dealer", "low",
      "unspecified Morgan Stanley entity: dealer unless the legal entity is a fund"),
    R(r"GOLDMAN SACHS", "GOLDMAN SACHS", "broker_dealer", "low",
      "unspecified Goldman Sachs entity: classified by legal entity, not assumed long-only"),
    R(r"CITIGROUP|CITIBANK", "CITIGROUP", "broker_dealer", "low", ""),
    R(r"JPMORGAN|J\.?P\.? ?MORGAN", "JPMORGAN", "broker_dealer", "low", ""),
    # ---- PE / VC / strategic ---------------------------------------------------------------------
    R(r"\bTPG\b", "TPG", "pe_vc_strategic", "high", ""),
    R(r"SOFTBANK|SVF |SB INVESTMENT", "SOFTBANK", "pe_vc_strategic", "high", ""),
    R(r"WARBURG|KKR|BLACKSTONE|CARLYLE|BAIN CAPITAL|ADVENT|GENERAL ATLANTIC|CHRYSCAPITAL|CHRYS CAPITAL|"
      r"MULTIPLES|TEMASEK HOLDINGS|PREMJI INVEST|PI OPPORTUNITIES|SEQUOIA|PEAK XV|ACCEL|TIGER GLOBAL|"
      r"PROSUS|NASPERS|MIH |ELEVATION|PRIVATE EQUITY|VENTURE|CAPITAL PARTNERS|HOLDINGS? \(MAURITIUS\)|"
      r"\bHBM\b|EVERSTONE|TRUE NORTH|INDIA VALUE FUND|LIGHTSPEED|MATRIX PARTNERS|B CAPITAL|FIDELITY GROWTH|"
      r"ANTFIN|ALIBABA|INFO EDGE|NAUKRI", None, "pe_vc_strategic", "medium", "PE/VC/strategic holder"),
    # ---- banks / broker-dealers ------------------------------------------------------------------
    R(r"\bBANK\b|FINANCIAL INSTITUTION", None, "bank", "low", ""),
    R(r"SECURITIES|BROKING|STOCK ?BROKERS?|CAPITAL MARKETS|SHARE ?BROKERS?|FINANCIAL SERVICES", None,
      "broker_dealer", "low", ""),
]
_COMPILED = [(re.compile(r.pattern, re.I), r) for r in RULES]

# SHP named-holder axes -> fallback category (used only when no rule matches)
AXIS_FALLBACK = {
    "MutualFunds": ("domestic_mf", "low"),
    "InsuranceCompanies": ("insurance", "low"),
    "ForeignPortfolioInvestor": ("unknown", "low"),     # never assume FPI = long-only
    "ForeignDirectInvestment": ("pe_vc_strategic", "low"),
    "ForeignCompanies": ("pe_vc_strategic", "low"),
    "AlternativeInvestmentFunds": ("unknown", "low"),
    "Banks": ("bank", "low"),
    "FinancialInstitution": ("bank", "low"),
    "ProvidentFundsOrPensionFunds": ("unknown", "low"),
    "SovereignWealthFunds": ("sovereign_wealth", "medium"),
    "VentureCapital": ("pe_vc_strategic", "low"),
}
# SEBI SHP Table II (promoter & promoter group) axes; the public table (III) uses different names
PROMOTER_AXES = {
    "DetailsSharesHeldByIndividualsOrHUFAxis", "DetailsOfSharesHeldByOthersIndianShareholdersAxis",
    "DetailsOfSharesHeldByCentralGovernmentOrStateGovernmentsAxis", "DetailsOfSharesHeldByIndianFinancialInstitutionsOrBanksAxis",
    "DetailsOfSharesHeldByNonResidentIndividualsOrForeignIndividualsAxis", "DetailsOfSharesHeldByOtherForeignShareholdersAxis",
    "DetailsOfSharesHeldByForeignGovernmentAxis", "DetailsOfSharesHeldByForeignInstitutionsAxis",
    "DetailsOfSharesHeldByForeignPortfolioInvestorAxis",
}
INSTITUTIONAL_AXIS_KEYS = ("MutualFunds", "Insurance", "ForeignPortfolio", "SovereignWealth", "Banks",
                           "FinancialInstitution", "ProvidentFunds", "AlternativeInvestment", "OtherInstitutions",
                           "ForeignVentureCapital", "VentureCapital", "InstitutionsForeign", "ForeignDirectInvestment",
                           "ForeignCompanies", "PersonsInConcertForPublic")

_SUFFIX_RE = re.compile(r"\b(LIMITED|LTD|PTE|PVT|PRIVATE|INC|LLC|LLP|PLC|SA|AG|CO|COMPANY|CORP|CORPORATION|"
                        r"THE|OF|A/C|AC)\b\.?")


def clean_name(name: str) -> str:
    s = re.sub(r"&AMP;", "&", str(name).upper())
    s = re.sub(r"[^A-Z0-9&() ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def family_from_name(name: str) -> str:
    s = clean_name(name)
    s = re.split(r"\b(A C|A/C|ACCOUNT)\b", s)[0]
    s = _SUFFIX_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return " ".join(s.split()[:4])


def classify(name: str, axis: str | None = None) -> dict:
    s = clean_name(name)
    if axis and (axis in PROMOTER_AXES or "Promoter" in axis):
        return {"family": family_from_name(name), "category": "promoter_group", "confidence": "high",
                "notes": "listed in promoter table"}
    for rx, r in _COMPILED:
        if rx.search(s):
            return {"family": r.family or family_from_name(name), "category": r.category,
                    "confidence": r.confidence, "notes": r.notes, "rule": r.pattern[:60]}
    if axis:
        for key, (cat, conf) in AXIS_FALLBACK.items():
            if key in axis:
                return {"family": family_from_name(name), "category": cat, "confidence": conf,
                        "notes": f"fallback from SHP axis {axis}"}
    return {"family": family_from_name(name), "category": "unknown", "confidence": "low", "notes": ""}


def classify_frame(df: pd.DataFrame, name_col: str = "name", axis_col: str | None = "axis") -> pd.DataFrame:
    keys = df[[name_col] + ([axis_col] if axis_col else [])].drop_duplicates()
    res = [classify(n, a if axis_col else None) for n, a in
           zip(keys[name_col], keys[axis_col] if axis_col else [None] * len(keys))]
    out = pd.concat([keys.reset_index(drop=True), pd.DataFrame(res)], axis=1)
    return df.merge(out, on=[name_col] + ([axis_col] if axis_col else []), how="left")


def is_institutional_axis(axis: str) -> bool:
    return any(k in str(axis) for k in INSTITUTIONAL_AXIS_KEYS)


SEED_NAMES = [
    # user-brief examples and case-study block-deal counterparties (news-reported names)
    "Norges Bank", "Government Pension Fund Global", "Fidelity Funds", "Small Cap World Fund", "Nippon India Mutual Fund",
    "Graviton Research Capital LLP", "AlphaGrep Securities Private Limited", "Goldman Sachs Funds - Goldman Sachs India Equity Portfolio",
    "Goldman Sachs (Singapore) Pte - ODI", "Goldman Sachs", "Societe Generale", "Polar Capital Funds", "Axis Mutual Fund",
    "Citigroup Global Markets Mauritius Private Limited", "Morgan Stanley Asia (Singapore) Pte", "Motilal Oswal Mutual Fund",
    "BNP Paribas Arbitrage", "Matthews Asia", "Optimas Capital", "TPG Asia VII SF Pte Ltd", "BlackRock", "Invesco Mutual Fund",
    "Aditya Birla Sun Life Mutual Fund", "SVF II Meerkat (DE) LLC", "BofA Securities Europe SA", "Kuwait Investment Authority",
    "Franklin Templeton", "HDFC Mutual Fund", "SBI Mutual Fund", "ICICI Prudential Mutual Fund", "Life Insurance Corporation of India",
]


def write_classification_db(observed: pd.DataFrame, out_path) -> pd.DataFrame:
    """Maintained investor-classification database: observed SHP names + seed names."""
    seed = pd.DataFrame([{**classify(n), "original_name": n, "source": "seed: user brief / news-reported deal counterparties",
                          "n_filings": 0} for n in SEED_NAMES])
    seed = seed.rename(columns={"family": "normalised_name", "category": "investor_category",
                                "confidence": "classification_confidence"})
    obs = observed.copy()
    cols = ["original_name", "normalised_name", "investor_category", "classification_confidence", "source", "notes",
            "n_filings", "first_seen", "last_seen", "shp_axis"]
    db = pd.concat([seed.reindex(columns=cols), obs.reindex(columns=cols)], ignore_index=True)
    db = db[db.investor_category != "promoter_group"]  # promoter individuals are not investor records
    db = db.drop_duplicates(["original_name", "investor_category"])
    db.to_csv(out_path, index=False)
    return db
