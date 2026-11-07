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


# Form 4 transaction codes -> readable type. Only "Purchase"/"Sale" pass the
# high-conviction filter (filter.py); the rest are kept for the full store.
CODE_TYPES = {
    "P": "Purchase", "S": "Sale",
    "A": "Award", "M": "Option Exercise", "C": "Conversion",
    "F": "Tax Withholding", "G": "Gift", "D": "Disposition to Issuer",
    "X": "Option Exercise", "W": "Will/Inheritance", "J": "Other (J)",
    "I": "Discretionary", "L": "Small Acquisition", "Z": "Trust",
    "E": "Expiration", "H": "Expiration", "O": "Option Exercise",
    "K": "Equity Swap", "U": "Tender of Shares", "V": "Voluntary Report",
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


_US_EXCH = ("NYSE", "NASDAQ", "NYSEAMERICAN", "NYSE AMERICAN", "NYSEARCA", "AMEX", "OTC", "OTCQX", "OTCQB")


def normalize_ticker(raw: Optional[str]) -> Optional[str]:
    """Clean the free-text issuerTradingSymbol filers type into one ticker:
    'NYSE: KRC' -> KRC, '(SIRI)' -> SIRI, 'LEN, LEN.B' -> LEN, 'MOGA/MOGB' -> MOGA,
    'N O G' -> NOG, 'vicr' -> VICR, 'none' -> None. Non-US exchange prefixes
    ('ASX:CRN') are left as they are so they never collide with a US ticker."""
    t = (raw or "").strip().strip("\"'()[]$ ").upper()
    if t in ("", "NONE", "N/A", "NA"):
        return None
    m = re.match(r"^([A-Z ]+?)\s*[:/]\s*(.+)$", t)
    if m and m.group(1).strip() in _US_EXCH:
        t = m.group(2).strip()
    elif ":" in t:
        return t                                   # e.g. ASX:CRN - keep, not a US ticker
    parts = [x for x in re.split(r"\s*(?:,|/|;|\s+AND\s+|\s+)\s*", t) if x]
    if not parts:
        return None
    if len(parts) > 1 and all(len(x) == 1 for x in parts):
        return "".join(parts)                      # 'N O G' -> NOG
    t = re.sub(r"[^A-Z0-9.\-]", "", parts[0])
    return t or None


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

    is_ten_pct = _text(owner, "reportingOwnerRelationship/isTenPercentOwner") == "1"

    role = _normalize_role(raw_role)
    if role is None and is_director:
        role = "Director"
    if role is None and is_officer:
        role = raw_role[:50] if raw_role else "Officer"
    # 10% owners / "other" filers: kept for the complete raw store
    # (insider_trades_all); the high-conviction filter drops them because
    # these labels are not in config.ROLES_TO_TRACK.
    if role is None and is_ten_pct:
        role = "10% Owner"
    if role is None:
        role = "Other"

    # Issuer ticker — EDGAR sometimes uses "none" as a placeholder
    _raw_ticker = (ticker or _text(root, "issuer/issuerTradingSymbol") or "").strip()
    issuer_ticker = normalize_ticker(_raw_ticker)
    issuer_name = _text(root, "issuer/issuerName") or company_name

    accession_fmt = f"{accession[:10]}-{accession[10:12]}-{accession[12:]}" if len(accession) == 18 else accession
    _url_cik = issuer_cik or cik.lstrip("0")
    filing_url = (
        f"https://www.sec.gov/Archives/edgar/data/{_url_cik}/"
        f"{accession.replace('-', '')}/{accession_fmt}-index.htm"
    )

    records = []

    for tx_index, tx in enumerate(root.findall(".//nonDerivativeTransaction")):
        # Code is under transactionCoding, NOT transactionAmounts.
        # Every code is returned (stored in full by insider_trades_all); the
        # high-conviction filter only keeps "Purchase" (P) and "Sale" (S).
        code = _text(tx, "transactionCoding/transactionCode")
        transaction_type = CODE_TYPES.get(code, f"Other ({code})" if code else "Other")

        shares_raw = _text(tx, "transactionAmounts/transactionShares/value")
        price_raw = _text(tx, "transactionAmounts/transactionPricePerShare/value")
        tx_date = _text(tx, "transactionDate/value")

        try:
            shares = float(shares_raw) if shares_raw else 0.0
            price = float(price_raw) if price_raw else 0.0
        except ValueError:
            continue

        total_value = shares * price
        owned_raw = _text(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
        try:
            owned_after = float(owned_raw) if owned_raw else None
        except ValueError:
            owned_after = None

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
            "transaction_code": code or None,
            "acquired_disposed": _text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value") or None,
            "shares_owned_after": owned_after,
            "direct_indirect": _text(tx, "ownershipNature/directOrIndirectOwnership/value") or None,
            "issuer_cik": issuer_cik or None,
            "tx_index": tx_index,
        })

    return records
