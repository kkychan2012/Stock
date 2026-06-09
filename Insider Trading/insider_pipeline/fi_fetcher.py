"""
Fetches insider transaction disclosures from Finansinspektionen (Swedish FSA)
Insynsregistret — used as a fallback for Swedish foreign private issuers such as
Ericsson (ERIC) that do not use Nasdaq Nordic for MAR Article 19 disclosures.

Search portal: https://marknadssok.fi.se/publiceringsklient
"""
import html as html_module
import logging
import re
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_SEARCH_URL = (
    "https://marknadssok.fi.se/Publiceringsklient/en-GB/Search/Search/Insyn"
)
_PAGE_SIZE = 10

_session = requests.Session()
_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
})

# FI position strings → normalised role
_ROLE_MAP = {
    "ceo":                   "CEO",
    "managing director":     "CEO",
    "cfo":                   "CFO",
    "chief financial":       "CFO",
    "coo":                   "COO",
    "deputy ceo":            "COO",
    "deputy managing":       "COO",
    "president":             "President",
    "chairman":              "Chairman",
    "board of directors":    "Director",
    "member of the board":   "Director",
    "supervisory body":      "Senior Officer",
    "management body":       "Senior Officer",
    "administrative":        "Senior Officer",
    "other member":          "Senior Officer",
    "employee representative": "Director",
    "director":              "Director",
    "svp":                   "SVP",
    "evp":                   "EVP",
    "vice president":        "VP",
}


def _normalize_role(raw: str) -> str:
    if not raw:
        return "Senior Officer"
    lower = raw.lower()
    for kw, role in _ROLE_MAP.items():
        if kw in lower:
            return role
    return "Senior Officer"


def _parse_number(s: str) -> float:
    if not s:
        return 0.0
    cleaned = re.sub(r"[^\d.,]", "", s.strip())
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _parse_date(s: str) -> Optional[str]:
    """Convert DD/MM/YYYY → YYYY-MM-DD."""
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _derive_search_name(edgar_name: str) -> str:
    """
    Extract a short search keyword from the EDGAR company name.
    FI search is a partial/contains match, so one distinctive word is enough.

    Examples:
      "ERICSSON LM TELEPHONE CO"  → "Ericsson"
      "NOKIA CORP"                → "Nokia"
      "VOLVO AB"                  → "Volvo"
    """
    _SKIP = {
        "CORP", "CORPORATION", "INC", "INCORPORATED", "LTD", "LIMITED",
        "OYJ", "AB", "ASA", "NV", "SA", "PLC", "SE", "AG", "BV",
        "CO", "LM", "THE", "OF", "AND",
    }
    words = [w for w in edgar_name.strip().upper().split() if w not in _SKIP and len(w) > 2]
    return words[0].title() if words else edgar_name.title()


def _parse_table(html: str) -> list[list[str]]:
    """Return data rows (excluding header) from the first HTML table."""
    table_m = re.search(r"<table[^>]*>(.*?)</table>", html, re.DOTALL)
    if not table_m:
        return []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_m.group(1), re.DOTALL)
    result = []
    for row in rows[1:]:  # skip header row
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)
        cleaned = [
            html_module.unescape(re.sub(r"<[^>]+>", "", c)).strip()
            for c in cells
        ]
        if cleaned:
            result.append(cleaned)
    return result


def fetch_fi_transactions(
    edgar_company_name: str,
    ticker: str,
    date_from: datetime,
    date_to: datetime,
) -> list[dict]:
    """
    Fetch insider (MAR Article 19) transactions from FI Insynsregistret.

    Returns a list of transaction dicts using the same schema as
    nasdaq_nordic_fetcher and sec_fetcher outputs.
    """
    search_name = _derive_search_name(edgar_company_name)
    logger.info("FI Insynsregistret: searching for '%s' (from '%s')", search_name, edgar_company_name)

    records: list[dict] = []
    page = 1

    while True:
        params = {
            "button":            "search",
            "SearchFunctionType": "Insyn",
            "Utgivare":          search_name,
            "Transaktionsdatum.From": date_from.strftime("%Y-%m-%d"),
            "Transaktionsdatum.To":   date_to.strftime("%Y-%m-%d"),
            "Page":              str(page),
            "language":          "en-gb",
        }
        try:
            resp = _session.get(_SEARCH_URL, params=params, timeout=20)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("FI search error (page %d): %s", page, exc)
            break

        rows = _parse_table(resp.text)
        if not rows:
            break

        for row in rows:
            rec = _row_to_record(row, ticker)
            if rec:
                records.append(rec)

        if len(rows) < _PAGE_SIZE:
            break  # last page
        page += 1

    if records:
        logger.info("FI Insynsregistret: %d acquisition(s) found for '%s'", len(records), search_name)
    else:
        logger.info("FI Insynsregistret: no qualifying transactions found for '%s'", search_name)

    return records


# Column indices in the FI table (English layout)
_COL = {
    "pub_date":      0,
    "issuer":        1,
    "name":          2,
    "position":      3,
    "closely_assoc": 4,
    "nature":        5,
    "instrument":    6,
    "instr_type":    7,
    "isin":          8,
    "tx_date":       9,
    "volume":        10,
    "unit":          11,
    "price":         12,
    "currency":      13,
}


def _row_to_record(row: list[str], ticker: str) -> Optional[dict]:
    if len(row) <= _COL["currency"]:
        return None

    nature = row[_COL["nature"]].lower()
    is_purchase = any(w in nature for w in ("acqui", "purch"))
    is_sale     = any(w in nature for w in ("dispos", "sale", "sold"))
    if not is_purchase and not is_sale:
        return None  # skip Allotment, transfers, etc.
    transaction_type = "Sale" if is_sale else "Purchase"

    price = _parse_number(row[_COL["price"]])
    if price == 0.0:
        return None  # skip free allotments disguised as acquisitions

    tx_date = _parse_date(row[_COL["tx_date"]])
    if not tx_date:
        return None

    pub_date = _parse_date(row[_COL["pub_date"]]) or tx_date
    volume = _parse_number(row[_COL["volume"]])
    if volume == 0.0:
        return None

    insider_name = row[_COL["name"]].strip()
    role = _normalize_role(row[_COL["position"]])
    company_name = row[_COL["issuer"]].strip()
    currency = row[_COL["currency"]].strip()

    # Convert SEK price to USD (approximate) for consistency with other sources
    # We keep the original currency in source field; total_value is in local currency
    total_value = volume * price

    return {
        "filed_date":       pub_date,
        "transaction_date": tx_date,
        "ticker":           ticker,
        "company_name":     company_name,
        "insider_name":     insider_name,
        "role":             role,
        "transaction_type": transaction_type,
        "shares":           volume,
        "price":            price,
        "total_value":      total_value,
        "flag_10b51":       False,
        "cluster_buy":      False,
        "filing_url":       "",
        "cik":              "",
        "accession":        "",
        "source":           f"FI ({currency})",
    }
