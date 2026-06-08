"""
Parse Form 6-K HTML documents for manager/insider transaction disclosures.
Targets EU MAR Article 19 format used by European foreign private issuers
(Nokia, ASML, Novo Nordisk, etc.).
"""
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 6-K filings that mention these keywords are likely manager transaction filings
_TRANSACTION_KEYWORDS = [
    "manager", "managerial", "article 19", "mar article",
    "pdmr", "person discharging", "notification requirement",
    "managers' transaction", "manager transaction",
    "notification of transaction", "insider transaction",
]

_ROLE_MAP = {
    "ceo": "CEO", "chief executive": "CEO",
    "cfo": "CFO", "chief financial": "CFO",
    "coo": "COO", "chief operating": "COO",
    "president": "President",
    "chairman": "Chairman",
    "board": "Director",
    "director": "Director",
    "member of": "Director",
    "supervisory": "Director",
    "pdmr": "Director",
    "evp": "EVP",
    "svp": "SVP",
    "vice president": "VP",
}

_ACQUISITION_WORDS = {"acquisition", "purchase", "buy", "bought", "acquired", "acquiring"}
_DISPOSAL_WORDS = {"disposal", "sale", "sold", "sell", "selling"}

_CURRENCY_RE = re.compile(r'\b(EUR|USD|GBP|SEK|NOK|DKK|CHF|JPY)\b', re.IGNORECASE)


# ── HTML stripping ─────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    text = re.sub(r'<tr[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<td[^>]*>', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'<p[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&#\d+;', ' ', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_manager_filing(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _TRANSACTION_KEYWORDS)


def _normalize_role(raw: str) -> Optional[str]:
    if not raw:
        return None
    lower = raw.lower()
    for kw, normalized in _ROLE_MAP.items():
        if kw in lower:
            return normalized
    return None


def _parse_number(s: str) -> float:
    if not s:
        return 0.0
    cleaned = re.sub(r'[^\d.,]', '', s.strip())
    # Handle European decimal comma: if last separator is comma and only one comma → decimal
    if cleaned.count(',') == 1 and '.' not in cleaned:
        cleaned = cleaned.replace(',', '.')
    else:
        cleaned = cleaned.replace(',', '')
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _find(text: str, *patterns: str) -> str:
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1).strip()
    return ""


def _normalise_date(raw: str) -> str:
    """Convert common date formats to YYYY-MM-DD."""
    if not raw:
        return ""
    raw = raw.replace('/', '-').strip()
    parts = raw.split('-')
    if len(parts) == 3:
        if len(parts[0]) == 4:          # YYYY-MM-DD
            return raw
        if len(parts[2]) == 4:          # DD-MM-YYYY
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return raw


# ── Block splitting ────────────────────────────────────────────────────────────

_BLOCK_HEADERS = re.compile(
    r'(?:Person subject to the notification|'
    r'MANAGERS[\'']?\s*TRANSACTIONS?|'
    r'NOTIFICATION OF TRANSACTIONS?|'
    r'Transaction\s+\d+\b)',
    re.IGNORECASE,
)


def _split_blocks(text: str) -> list[str]:
    """Split a multi-transaction 6-K into per-transaction blocks."""
    positions = [m.start() for m in _BLOCK_HEADERS.finditer(text)]
    if not positions:
        return [text]
    blocks = []
    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        blocks.append(text[start:end])
    return blocks


# ── Per-block parser ───────────────────────────────────────────────────────────

def _parse_block(text: str, filed_date: str, company_name: str,
                 ticker: Optional[str], cik: str,
                 accession_fmt: str, filing_url: str) -> Optional[dict]:

    lower = text.lower()

    # Must contain an acquisition keyword
    if not any(w in lower for w in _ACQUISITION_WORDS):
        return None
    # Skip pure disposals
    if any(w in lower for w in _DISPOSAL_WORDS) and not any(w in lower for w in _ACQUISITION_WORDS):
        return None
    # Confirm nature = acquisition (not disposal that also mentions the word in a different context)
    nature_raw = _find(text,
        r'Nature of (?:the )?transaction[:\s]+([^\n]{2,60})',
        r'Type of transaction[:\s]+([^\n]{2,60})',
        r'Transaction type[:\s]+([^\n]{2,60})',
    )
    if nature_raw:
        nature_lower = nature_raw.lower()
        if any(w in nature_lower for w in _DISPOSAL_WORDS):
            return None

    # ── Insider name ──────────────────────────────────────────────────────────
    insider_name = _find(text,
        r'Name[:\s]+([A-Z][a-zA-ZÀ-ɏ][\w\s\-\.]{2,50}?)(?:\n|Position|Role|Title|Initial)',
        r'(?:Person[^:\n]*:|Notifier\s*:)\s*([A-Z][a-zA-ZÀ-ɏ][\w\s\-\.]{2,50})(?:\n|,)',
        r'Name of PDMR[:\s]+([A-Z][\w\s\-\.]{2,50})(?:\n|,)',
    )

    # ── Role ──────────────────────────────────────────────────────────────────
    raw_role = _find(text,
        r'Position[/\s]*(?:status)?[:\s]+([^\n,]{3,80}?)(?:\n|$|,|\()',
        r'(?:Title|Role|Function|Capacity)[:\s]+([^\n,]{3,80}?)(?:\n|$|,)',
    )
    role = _normalize_role(raw_role)
    if not role:
        # Fall back: scan first 300 chars of block for any role keyword
        role = _normalize_role(text[:300])
    if not role:
        return None

    # ── Transaction date ──────────────────────────────────────────────────────
    tx_date_raw = _find(text,
        r'Date of (?:the )?transaction[:\s]+(\d{1,4}[-/]\d{1,2}[-/]\d{2,4})',
        r'Transaction date[:\s]+(\d{1,4}[-/]\d{1,2}[-/]\d{2,4})',
        r'Date[:\s]+(\d{4}[-/]\d{2}[-/]\d{2})',
    )
    tx_date = _normalise_date(tx_date_raw) or filed_date

    # ── Shares / volume ───────────────────────────────────────────────────────
    shares_raw = _find(text,
        r'(?:Number of shares?|Volume|Quantity)[:\s]+([\d\s,\.]+?)(?:\s*(?:shares?|ADS|ADR)|\n|$)',
        r'([\d][\d\s,\.]+)\s+(?:shares?|ADS)\b',
    )
    shares = _parse_number(shares_raw)

    # ── Price ─────────────────────────────────────────────────────────────────
    price_raw = _find(text,
        r'(?:Price|Unit price|Weighted average price)[:\s]+([\d\s,\.]+)\s*(?:EUR|USD|GBP|SEK|NOK|DKK)',
        r'(?:at|@)\s+([\d,\.]+)\s*(?:EUR|USD|GBP|SEK|NOK|DKK)',
        r'Price[:\s]+([\d,\.]+)',
    )
    price = _parse_number(price_raw)

    # ── Total value ───────────────────────────────────────────────────────────
    total_raw = _find(text,
        r'(?:Total volume|Aggregate (?:amount|volume)|Total value|Volume)[:\s]+([\d\s,\.]+)\s*(?:EUR|USD|GBP)',
    )
    total_value = shares * price
    if total_value == 0 and total_raw:
        total_value = _parse_number(total_raw)

    if shares == 0 and total_value == 0:
        return None

    return {
        "filed_date":        filed_date,
        "transaction_date":  tx_date,
        "ticker":            ticker,
        "company_name":      company_name,
        "insider_name":      insider_name or "Unknown",
        "role":              role,
        "transaction_type":  "Purchase",
        "shares":            shares,
        "price":             price,
        "total_value":       total_value,
        "flag_10b51":        False,
        "cluster_buy":       False,
        "filing_url":        filing_url,
        "cik":               cik,
        "accession":         accession_fmt,
        "source":            "6-K",
    }


# ── Public entry point ─────────────────────────────────────────────────────────

def parse_form6k(html_text: str, cik: str, accession: str,
                 filed_date: str, company_name: str,
                 ticker: Optional[str] = None,
                 issuer_cik: str = None) -> list[dict]:
    """
    Parse a Form 6-K document for EU MAR manager transaction disclosures.
    Returns a list of transaction dicts (same schema as parse_form4 output).
    """
    plain = _strip_html(html_text)

    if not _is_manager_filing(plain):
        return []

    accession_nodash = accession.replace("-", "")
    accession_fmt = (
        f"{accession_nodash[:10]}-{accession_nodash[10:12]}-{accession_nodash[12:]}"
        if len(accession_nodash) == 18 else accession_nodash
    )
    _url_cik = issuer_cik or cik.lstrip("0")
    filing_url = (
        f"https://www.sec.gov/Archives/edgar/data/{_url_cik}/"
        f"{accession_nodash}/{accession_fmt}-index.htm"
    )

    blocks = _split_blocks(plain)
    records = []
    for block in blocks:
        rec = _parse_block(block, filed_date, company_name, ticker,
                           cik, accession_fmt, filing_url)
        if rec:
            records.append(rec)

    # If block splitting yielded nothing, try the whole document as one block
    if not records and len(blocks) <= 1:
        rec = _parse_block(plain, filed_date, company_name, ticker,
                           cik, accession_fmt, filing_url)
        if rec:
            records.append(rec)

    logger.debug("Form 6-K %s/%s: %d transactions parsed", cik, accession, len(records))
    return records
