"""
Parse Form 4 XML into structured transaction records.
"""
import logging
import re
from typing import Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

ROLE_KEYWORDS = {
    "ceo": "CEO",
    "chief executive": "CEO",
    "cfo": "CFO",
    "chief financial": "CFO",
    "coo": "COO",
    "chief operating": "COO",
    "president": "President",
    "chairman": "Chairman",
    "director": "Director",
}


def _normalize_role(raw: str) -> Optional[str]:
    if not raw:
        return None
    lower = raw.lower()
    for keyword, normalized in ROLE_KEYWORDS.items():
        if keyword in lower:
            return normalized
    return None


def _text(element: ET.Element, path: str) -> str:
    node = element.find(path)
    return (node.text or "").strip() if node is not None else ""


def _has_10b51(xml_text: str) -> bool:
    """Check if any footnote or text in the filing mentions a 10b5-1 plan."""
    return bool(re.search(r"10b5-?1", xml_text, re.IGNORECASE))


def parse_form4(xml_text: str, cik: str, accession: str,
                filed_date: str, company_name: str, ticker: Optional[str],
                issuer_cik: str = None) -> list[dict]:
    """
    Parse a Form 4 XML and return a list of transaction dicts.
    Returns [] if parsing fails or no valid transactions found.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("XML parse error for %s/%s: %s", cik, accession, exc)
        return []

    flag_10b51 = _has_10b51(xml_text)

    # Reporter info
    owner = root.find(".//reportingOwner")
    if owner is None:
        return []

    insider_name = _text(owner, "reportingOwnerId/rptOwnerName")
    raw_role = _text(owner, "reportingOwnerRelationship/officerTitle")
    is_director = _text(owner, "reportingOwnerRelationship/isDirector") == "1"
    is_officer = _text(owner, "reportingOwnerRelationship/isOfficer") == "1"

    role = _normalize_role(raw_role)
    if role is None and is_director:
        role = "Director"
    if role is None and is_officer:
        role = raw_role[:50] if raw_role else "Officer"

    if not role:
        return []

    # Issuer ticker — EDGAR sometimes uses "none" as a placeholder
    _raw_ticker = (ticker or _text(root, "issuer/issuerTradingSymbol") or "").strip()
    issuer_ticker = None if _raw_ticker.lower() in ("none", "n/a", "") else _raw_ticker
    issuer_name = _text(root, "issuer/issuerName") or company_name

    accession_fmt = f"{accession[:10]}-{accession[10:12]}-{accession[12:]}" if len(accession) == 18 else accession
    _url_cik = issuer_cik or cik.lstrip("0")
    filing_url = (
        f"https://www.sec.gov/Archives/edgar/data/{_url_cik}/"
        f"{accession.replace('-', '')}/{accession_fmt}-index.htm"
    )

    records = []

    for tx in root.findall(".//nonDerivativeTransaction"):
        # Code is under transactionCoding, NOT transactionAmounts
        code = _text(tx, "transactionCoding/transactionCode")
        if code == "P":
            transaction_type = "Purchase"
        elif code == "S":
            transaction_type = "Sale"
        else:
            continue

        shares_raw = _text(tx, "transactionAmounts/transactionShares/value")
        price_raw = _text(tx, "transactionAmounts/transactionPricePerShare/value")
        tx_date = _text(tx, "transactionDate/value")

        try:
            shares = float(shares_raw) if shares_raw else 0.0
            price = float(price_raw) if price_raw else 0.0
        except ValueError:
            continue

        total_value = shares * price

        records.append({
            "filed_date": filed_date,
            "transaction_date": tx_date,
            "ticker": issuer_ticker,
            "company_name": issuer_name,
            "insider_name": insider_name,
            "role": role,
            "transaction_type": transaction_type,
            "shares": shares,
            "price": price,
            "total_value": total_value,
            "flag_10b51": flag_10b51,
            "cluster_buy": False,
            "filing_url": filing_url,
            "cik": cik,
            "accession": accession,
            "source": "Form 4",
        })

    return records
