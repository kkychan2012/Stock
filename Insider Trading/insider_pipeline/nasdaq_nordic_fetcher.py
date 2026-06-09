"""
Fetches and parses manager transaction disclosures from the Nasdaq Nordic
announcement API (covers Helsinki, Stockholm, Copenhagen, Baltic exchanges).

Used as a fallback when a ticker has no SEC Form 4 or Form 6-K filings
(i.e. the company is a Nordic foreign private issuer).
"""
import re
import logging
import requests
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ── API constants ──────────────────────────────────────────────────────────────

_API_URL     = "https://api.news.eu.nasdaq.com/news/query.action"
_VIEWER_BASE = "https://view.news.eu.nasdaq.com/view"
_CATEGORY_MANAGERS_TRANSACTIONS = 66
_PAGE_SIZE   = 100

# Markets to try in order (Helsinki first for NOK, etc.)
_MARKETS = [
    "Main Market, Helsinki",
    "First North, Helsinki",
    "Main Market, Stockholm",
    "First North, Stockholm",
    "Main Market, Copenhagen",
    "First North, Copenhagen",
]

# EDGAR company name → Nasdaq Nordic display name
_NAME_MAP = {
    "NOKIA":              "Nokia",
    "NOKIA CORP":         "Nokia",
    "NOKIA OYJ":          "Nokia",
    "ERICSSON":           "Ericsson",
    "TELEFONAKTIEBOLAGET LM ERICSSON": "Ericsson",
    "NOVO NORDISK":       "Novo Nordisk",
    "NOVO NORDISK A/S":   "Novo Nordisk",
    "VESTAS":             "Vestas Wind Systems",
    "KONE":               "KONE",
    "KONE OYJ":           "KONE",
    "NESTE":              "Neste",
    "NESTE OYJ":          "Neste",
    "WARTSILA":           "Wärtsilä",
    "WARTSILA OYJ":       "Wärtsilä",
    "STORA ENSO":         "Stora Enso",
    "UPM":                "UPM",
    "UPM-KYMMENE":        "UPM",
    "METSO":              "Metso",
    "FORTUM":             "Fortum",
    "ELISA":              "Elisa",
    "NORDEA":             "Nordea Bank",
    "SAMPO":              "Sampo",
    "ORION":              "Orion",
}

# Common corporate suffixes to strip when deriving a name
_SUFFIXES = {
    "CORP", "CORPORATION", "INC", "INCORPORATED", "LTD", "LIMITED",
    "OYJ", "AB", "ASA", "NV", "SA", "PLC", "SE", "AG", "BV",
}

# Role normalization (same as form6k_parser)
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
    "other senior": "Senior Officer",
    "senior vice": "SVP",
    "vice president": "VP",
    "evp": "EVP",
    "svp": "SVP",
}


# ── Name normalisation ─────────────────────────────────────────────────────────

def normalize_company_name(edgar_name: str) -> str:
    """Map an EDGAR company name to the Nasdaq Nordic display name."""
    upper = edgar_name.strip().upper()
    # Exact/prefix match against known names
    for key, val in _NAME_MAP.items():
        if upper == key or upper.startswith(key + " "):
            return val
    # Strip suffixes and title-case the remainder
    words = [w for w in upper.split() if w not in _SUFFIXES and len(w) > 1]
    return " ".join(w.title() for w in words) if words else edgar_name.title()


# ── HTTP helper (no SEC rate limiting needed for Nasdaq API) ───────────────────

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; insider-pipeline/1.0)",
    "Accept": "application/json, text/html, */*",
})


def _get(url: str, params: dict = None, timeout: int = 30) -> requests.Response:
    resp = _session.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp


# ── Disclosure fetcher ─────────────────────────────────────────────────────────

def fetch_manager_transactions(edgar_company_name: str, ticker: str,
                                date_from: datetime, date_to: datetime) -> list[dict]:
    """
    Fetch and parse manager transaction disclosures from Nasdaq Nordic.

    Tries each Nordic market in turn until results are found.
    Returns a list of transaction dicts (same schema as parse_form4 output).
    """
    nasdaq_name = normalize_company_name(edgar_company_name)
    logger.info("Nasdaq Nordic: searching for '%s' (from '%s')", nasdaq_name, edgar_company_name)

    for market in _MARKETS:
        disclosures = _fetch_disclosures_for_market(nasdaq_name, market, date_from, date_to)
        if disclosures:
            logger.info("  Found %d manager transaction disclosures on %s", len(disclosures), market)
            records = []
            for d in disclosures:
                recs = _fetch_and_parse_disclosure(d, ticker)
                records.extend(recs)
            return records

    logger.info("  No Nasdaq Nordic manager transactions found for '%s'", nasdaq_name)
    return []


def _fetch_disclosures_for_market(company_name: str, market: str,
                                   date_from: datetime, date_to: datetime) -> list[dict]:
    """Query one Nasdaq Nordic market for manager transaction disclosures."""
    results = []
    page = 1

    while True:
        params = {
            "type": "json",
            "showpage": page,
            "market": market,
            "lang": "en",
            "company": company_name,
            "categoryId": _CATEGORY_MANAGERS_TRANSACTIONS,
            "limit": _PAGE_SIZE,
        }
        try:
            resp = _get(_API_URL, params=params)
            data = resp.json()
        except Exception as exc:
            logger.debug("Nasdaq Nordic API error (market=%s, page=%d): %s", market, page, exc)
            break

        items = data.get("results", {}).get("item", [])
        if not items:
            break

        oldest_on_page: datetime = None
        for item in items:
            if item.get("categoryId") != _CATEGORY_MANAGERS_TRANSACTIONS:
                continue
            release_str = (item.get("releaseTime") or item.get("published", ""))[:19]
            try:
                release_dt = datetime.strptime(release_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if oldest_on_page is None or release_dt < oldest_on_page:
                oldest_on_page = release_dt
            # Inclusive date filter (add 1-day buffer on upper end for timezone)
            if not (date_from <= release_dt <= date_to + timedelta(days=1)):
                continue
            msg_url = item.get("messageUrl", "")
            if not msg_url:
                continue
            results.append({
                "disclosure_id": item.get("disclosureId"),
                "headline":      item.get("headline", ""),
                "message_url":   msg_url,
                "filed_date":    release_str[:10],
                "company":       item.get("company", company_name),
                "market":        market,
            })

        # Items are returned newest-first; stop once we've gone past date_from
        total = int(data.get("count") or 0)
        if oldest_on_page and oldest_on_page < date_from:
            break  # All remaining items are older than our window
        if page * _PAGE_SIZE >= total or len(items) < _PAGE_SIZE:
            break
        page += 1

    return results


# ── Disclosure document fetcher + parser ───────────────────────────────────────

def _fetch_and_parse_disclosure(disclosure: dict, ticker: str) -> list[dict]:
    """Download and parse one Nasdaq Nordic manager transaction disclosure."""
    url = disclosure["message_url"]
    try:
        resp = _get(url)
        html = resp.text
    except Exception as exc:
        logger.warning("Failed to fetch disclosure %s: %s", disclosure.get("disclosure_id"), exc)
        return []

    return _parse_disclosure(html, disclosure, ticker)


def _strip_html(html: str) -> str:
    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    text = re.sub(r'<tr[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<td[^>]*>', '\t', text, flags=re.IGNORECASE)
    text = re.sub(r'<th[^>]*>', '\t', text, flags=re.IGNORECASE)
    text = re.sub(r'<p[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&#\d+;', ' ', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _find(text: str, *patterns: str) -> str:
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1).strip()
    return ""


def _parse_number(s: str) -> float:
    if not s:
        return 0.0
    cleaned = re.sub(r'[^\d.,]', '', s.strip())
    if cleaned.count(',') == 1 and '.' not in cleaned:
        cleaned = cleaned.replace(',', '.')
    else:
        cleaned = cleaned.replace(',', '')
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _normalize_role(raw: str) -> Optional[str]:
    if not raw:
        return None
    lower = raw.lower()
    for kw, norm in _ROLE_MAP.items():
        if kw in lower:
            return norm
    return None


def _parse_disclosure(html: str, disclosure: dict, ticker: str) -> list[dict]:
    """
    Parse a Nasdaq Nordic manager transaction disclosure (EU MAR Article 19 format).

    Actual format observed from view.news.eu.nasdaq.com:
      Name: Hanrahan, Victoria
      Position: Other senior manager
      ____________________________________________
      Transaction date: 2026-05-26
      Nature of the transaction: ACQUISITION
      Transaction details
      (1): Volume: 22713 Unit price: 16.0179 USD
      ____________________________________________
    """
    plain = _strip_html(html)
    filed_date = disclosure["filed_date"]
    company_name = disclosure["company"]
    filing_url = disclosure["message_url"]

    # ── Person name ────────────────────────────────────────────────────────────
    insider_name = _find(plain,
        r'Name:\s*([A-ZÀ-Ý][^\n]{2,60}?)(?:\n|Position)',
        r'Name:\s*([^\n]{2,60})',
    )
    # ESMA format uses "Surname, Firstname" → normalise to "Firstname Surname"
    if insider_name and ',' in insider_name:
        parts = [p.strip() for p in insider_name.split(',', 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            insider_name = f"{parts[1]} {parts[0]}"

    # ── Role ───────────────────────────────────────────────────────────────────
    raw_role = _find(plain,
        r'Position:\s*([^\n]{3,80})',
        r'Position/status:\s*([^\n]{3,80})',
    )
    role = _normalize_role(raw_role)
    if not role:
        role = _normalize_role(plain[:800])
    if not role:
        role = "Senior Officer"  # EU MAR: only senior managers need to disclose

    # ── Split into per-transaction blocks ─────────────────────────────────────
    # Blocks are separated by lines of underscores (at least 10 underscores)
    separator_re = re.compile(r'_{8,}', re.MULTILINE)
    raw_blocks = separator_re.split(plain)

    records = []
    for block in raw_blocks:
        rec = _extract_transaction(block, filed_date, company_name, ticker,
                                    filing_url, disclosure, insider_name, role)
        if rec:
            records.append(rec)

    if not records:
        logger.debug("Nasdaq Nordic: no acquisitions in %s (%s)",
                     disclosure.get("disclosure_id"), disclosure.get("headline"))
    return records


def _extract_transaction(block: str, filed_date: str, company_name: str,
                          ticker: str, filing_url: str, disclosure: dict,
                          insider_name: str, role: str) -> Optional[dict]:
    """Extract one transaction record from a single block of text."""
    # Must have a transaction date
    tx_date_raw = _find(block,
        r'Transaction date:\s*(\d{4}-\d{2}-\d{2})',
        r'Transaction date:\s*(\d{2}[./]\d{2}[./]\d{4})',
    )
    if not tx_date_raw:
        return None

    # Normalise date format
    tx_date = tx_date_raw.replace('/', '-').replace('.', '-')
    if len(tx_date) == 10 and tx_date[2] == '-':  # DD-MM-YYYY → YYYY-MM-DD
        d, m, y = tx_date.split('-')
        tx_date = f"{y}-{m}-{d}"

    # Nature must be ACQUISITION or DISPOSAL (skip grants, transfers, etc.)
    nature = _find(block, r'Nature of the transaction:\s*([^\n]{3,40})')
    if not nature:
        return None
    nat_lower = nature.lower()
    is_sale     = any(w in nat_lower for w in ("disposal", "sale", "sold"))
    is_purchase = any(w in nat_lower for w in ("acquisition", "purchase", "buy", "subscri"))
    if not is_sale and not is_purchase:
        return None
    transaction_type = "Sale" if is_sale else "Purchase"

    # Volume and price — the format is:
    #   (1): Volume: 22713 Unit price: 16.0179 USD
    # or aggregated:
    #   (1): Volume: 22713 Volume weighted average price: 16.0179 USD
    vol_price_re = re.compile(
        r'\(\d+\):\s*Volume:\s*([\d\s,\.]+?)\s+'
        r'(?:Unit price|Volume weighted average price):\s*([\d,\.]+)\s*'
        r'(USD|EUR|GBP|SEK|NOK|DKK|CHF)',
        re.IGNORECASE,
    )
    match = vol_price_re.search(block)
    if not match:
        # Fall back: aggregated section "Volume: 44682 Volume weighted average price: ..."
        match = re.search(
            r'Volume:\s*([\d\s,\.]+?)\s+'
            r'Volume weighted average price:\s*([\d,\.]+)\s*(USD|EUR|GBP|SEK|NOK|DKK|CHF)',
            block, re.IGNORECASE,
        )
    if not match:
        return None

    shares = _parse_number(match.group(1))
    price = _parse_number(match.group(2))
    total_value = shares * price
    if total_value == 0:
        return None

    return {
        "filed_date":       filed_date,
        "transaction_date": tx_date,
        "ticker":           ticker,
        "company_name":     company_name,
        "insider_name":     insider_name or "Unknown",
        "role":             role,
        "transaction_type": transaction_type,
        "shares":           shares,
        "price":            price,
        "total_value":      total_value,
        "flag_10b51":       False,
        "cluster_buy":      False,
        "filing_url":       filing_url,
        "cik":              "",
        "accession":        str(disclosure.get("disclosure_id", "")),
        "source":           "Nasdaq Nordic",
    }
