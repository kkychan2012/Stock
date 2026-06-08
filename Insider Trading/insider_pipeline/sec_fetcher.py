"""
Handles all SEC EDGAR API calls with rate limiting and retries.
"""
import re
import time
import logging
import threading
import requests
from datetime import datetime, timedelta
from typing import Optional

from config import (
    SEC_USER_AGENT, SEC_RATE_LIMIT, SEC_RETRY_ATTEMPTS,
    SEC_RETRY_BACKOFF, EDGAR_SUBMISSIONS_URL, EDGAR_FILING_BASE,
)

logger = logging.getLogger(__name__)

_rate_lock = threading.Lock()
_request_times: list[float] = []


def _throttle():
    """Enforce max 10 requests/second sliding window (lock-safe for threads)."""
    while True:
        with _rate_lock:
            now = time.monotonic()
            cutoff = now - 1.0
            while _request_times and _request_times[0] < cutoff:
                _request_times.pop(0)
            if len(_request_times) < SEC_RATE_LIMIT:
                _request_times.append(time.monotonic())
                return  # got a slot
            # Calculate wait time, then release lock before sleeping
            sleep_for = max(0.0, 1.0 - (now - _request_times[0])) + 0.01
        time.sleep(sleep_for)  # lock is NOT held here


def _get(url: str, params: dict = None, stream: bool = False,
         no_retry_on: tuple = (404, 403)) -> requests.Response:
    headers = {"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    for attempt in range(SEC_RETRY_ATTEMPTS):
        try:
            _throttle()
            resp = requests.get(url, headers=headers, params=params,
                                timeout=30, stream=stream)
            if resp.status_code == 429:
                wait = SEC_RETRY_BACKOFF ** (attempt + 1)
                logger.warning("Rate-limited by SEC, waiting %ss", wait)
                time.sleep(wait)
                continue
            # Don't retry permanent client errors
            if resp.status_code in no_retry_on:
                resp.raise_for_status()
            resp.raise_for_status()
            return resp
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in no_retry_on:
                raise
            wait = SEC_RETRY_BACKOFF ** (attempt + 1)
            logger.warning("Request failed (%s), retry %d/%d in %ss: %s",
                           url, attempt + 1, SEC_RETRY_ATTEMPTS, wait, exc)
            if attempt < SEC_RETRY_ATTEMPTS - 1:
                time.sleep(wait)
        except requests.RequestException as exc:
            wait = SEC_RETRY_BACKOFF ** (attempt + 1)
            logger.warning("Request failed (%s), retry %d/%d in %ss: %s",
                           url, attempt + 1, SEC_RETRY_ATTEMPTS, wait, exc)
            if attempt < SEC_RETRY_ATTEMPTS - 1:
                time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url} after {SEC_RETRY_ATTEMPTS} attempts")


def _fetch_chunk(chunk_from: datetime, chunk_to: datetime) -> list[dict]:
    """Fetch one date-chunk of Form 4 filings from EDGAR EFTS."""
    results = []
    start = 0
    page_size = 100
    MAX_OFFSET = 9900  # Elasticsearch hard cap

    while True:
        params = {
            "forms": "4",
            "dateRange": "custom",
            "startdt": chunk_from.strftime("%Y-%m-%d"),
            "enddt": chunk_to.strftime("%Y-%m-%d"),
            "from": start,
            "size": page_size,
        }
        try:
            resp = _get("https://efts.sec.gov/LATEST/search-index", params=params)
            data = resp.json()
        except Exception as exc:
            logger.error("EFTS fetch error at offset %d (%s-%s): %s",
                         start, chunk_from.date(), chunk_to.date(), exc)
            break

        hits_block = data.get("hits", {})
        hits = hits_block.get("hits", [])
        if not hits:
            break

        for h in hits:
            src = h.get("_source", {})
            # EDGAR EFTS uses 'adsh' (not 'accession_no') for the accession number
            adsh = src.get("adsh", "")
            if not adsh:
                continue
            accession = adsh.replace("-", "")
            if len(accession) < 16:
                continue
            # Filer CIK = first 10 digits of the accession number
            cik = accession[:10]

            # EDGAR stores Form4 XML under the issuer's CIK, not the filer's CIK.
            # The last element of 'ciks' is the issuer; the others are reporting owners.
            all_ciks = src.get("ciks", [])
            issuer_cik = all_ciks[-1].lstrip("0") if all_ciks else cik.lstrip("0")

            # Company name: last entry in display_names, strip the "(CIK ...)" suffix
            display_names = src.get("display_names", [])
            company_name = ""
            if display_names:
                company_name = display_names[-1].split("(CIK")[0].strip()

            results.append({
                "cik": cik,           # filer CIK (for index URL fallback)
                "issuer_cik": issuer_cik,  # issuer CIK (for direct XML URL)
                "accession": accession,
                "filed_date": src.get("file_date", ""),
                "company_name": company_name,
            })

        total = hits_block.get("total", {})
        total_count = total.get("value", 0) if isinstance(total, dict) else int(total or 0)
        start += page_size
        if start >= min(total_count, MAX_OFFSET):
            break

    return results


def fetch_form4_index(date_from: datetime, date_to: datetime) -> list[dict]:
    """
    Pull Form 4 accession numbers from the EDGAR full-text search API.
    Splits into 7-day chunks to stay under ES 10K result limit.
    Returns list of dicts: {cik, accession, filed_date, company_name}.
    """
    results = []
    chunk_size = timedelta(days=4)  # keeps each chunk <2000 results; avoids EFTS 500s at high offsets
    cursor = date_from

    while cursor < date_to:
        chunk_end = min(cursor + chunk_size, date_to)
        logger.debug("Fetching chunk %s -> %s", cursor.date(), chunk_end.date())
        chunk = _fetch_chunk(cursor, chunk_end)
        results.extend(chunk)
        cursor = chunk_end + timedelta(days=1)

    logger.info("Index returned %d Form 4 entries", len(results))
    return results


def fetch_form4_xml(cik: str, accession: str, issuer_cik: str = None) -> Optional[str]:
    """
    Download the Form 4 XML content.
    EDGAR stores Form 4 XML under the issuer's CIK directory, not the filer's.
    Uses index-first strategy: fetch the filing index to find the exact XML URL.
    """
    accession_nodash = accession.replace("-", "")
    filer_cik_nodash = cik.lstrip("0")
    accession_fmt = f"{accession_nodash[:10]}-{accession_nodash[10:12]}-{accession_nodash[12:]}"

    # Strategy 1: direct URL via issuer CIK (most common pattern)
    if issuer_cik:
        direct_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{issuer_cik}/{accession_nodash}/primary_doc.xml"
        )
        try:
            resp = _get(direct_url)
            if "<ownershipDocument" in resp.text:
                return resp.text
        except Exception:
            pass

    # Strategy 2: fetch the filing index to discover the exact XML path.
    # Try issuer CIK first (EDGAR stores Form 4s under the issuer), then filer CIK.
    index_ciks = []
    if issuer_cik and issuer_cik != filer_cik_nodash:
        index_ciks.append(issuer_cik)
    index_ciks.append(filer_cik_nodash)

    for idx_cik in index_ciks:
        index_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{idx_cik}/{accession_nodash}/{accession_fmt}-index.htm"
        )
        try:
            idx_resp = _get(index_url)
            xml_paths = re.findall(
                r'href="(/Archives/edgar/data/[^"]+\.xml)"', idx_resp.text
            )
            xml_paths = [p for p in xml_paths if "xslF345X" not in p]
            for path in xml_paths:
                try:
                    xml_resp = _get("https://www.sec.gov" + path)
                    if "<ownershipDocument" in xml_resp.text:
                        return xml_resp.text
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("Index fetch failed for cik=%s %s/%s: %s",
                         idx_cik, cik, accession, exc)

    return None


def fetch_company_ticker(cik: str) -> Optional[str]:
    """Look up ticker symbol from SEC submissions JSON."""
    padded = str(cik).zfill(10)
    url = EDGAR_SUBMISSIONS_URL.format(cik=padded)
    try:
        resp = _get(url)
        data = resp.json()
        tickers = data.get("tickers", [])
        return tickers[0] if tickers else None
    except Exception as exc:
        logger.debug("Ticker lookup failed for CIK %s: %s", cik, exc)
        return None


# ── Ticker-specific helpers ───────────────────────────────────────────────────

_ticker_to_cik: dict[str, str] = {}


def fetch_cik_for_ticker(ticker: str) -> Optional[str]:
    """
    Resolve a stock ticker to its EDGAR CIK using SEC's company_tickers.json.
    Result is cached in-process.
    """
    global _ticker_to_cik
    if not _ticker_to_cik:
        try:
            resp = _get("https://www.sec.gov/files/company_tickers.json")
            data = resp.json()
            _ticker_to_cik = {
                v["ticker"].upper(): str(v["cik_str"])
                for v in data.values()
            }
            logger.info("Loaded %d tickers from EDGAR company_tickers.json", len(_ticker_to_cik))
        except Exception as exc:
            logger.error("Failed to load company tickers map: %s", exc)
            return None
    return _ticker_to_cik.get(ticker.upper().strip())


def _extract_form4_entries(recent: dict, issuer_cik: str, company_name: str,
                            date_from: datetime, date_to: datetime) -> list[dict]:
    """Parse a submissions 'recent' or archive block for Form 4 entries."""
    results = []
    for acc, form, date_str, primary_doc in zip(
        recent.get("accessionNumber", []),
        recent.get("form", []),
        recent.get("filingDate", []),
        recent.get("primaryDocument", []),
    ):
        if form not in ("4", "4/A"):
            continue
        try:
            filed_dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        if not (date_from <= filed_dt <= date_to):
            continue
        accession_nodash = acc.replace("-", "")
        results.append({
            "cik": accession_nodash[:10],       # filer CIK (for index fallback)
            "issuer_cik": issuer_cik.lstrip("0"),
            "accession": accession_nodash,
            "filed_date": date_str,
            "company_name": company_name,
            "primary_doc": primary_doc,          # exact filename — no guessing needed
        })
    return results


def fetch_form4_by_company(issuer_cik: str, date_from: datetime,
                            date_to: datetime) -> list[dict]:
    """
    Get all Form 4 filings for a company by CIK using the EDGAR submissions API.
    The submissions JSON for the issuer lists Form 4s where it is the issuer.
    Also walks older archive pages if the date range extends beyond recent filings.
    """
    padded = str(issuer_cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{padded}.json"
    try:
        resp = _get(url)
        data = resp.json()
    except Exception as exc:
        logger.error("Submissions fetch failed for CIK %s: %s", issuer_cik, exc)
        return []

    company_name = data.get("name", "")
    results = _extract_form4_entries(
        data.get("filings", {}).get("recent", {}),
        issuer_cik, company_name, date_from, date_to,
    )

    # Older archive pages (each covers ~1 000 filings, listed newest-first)
    for file_info in data.get("filings", {}).get("files", []):
        archive_end = file_info.get("date", "")
        if archive_end and datetime.strptime(archive_end, "%Y-%m-%d") < date_from:
            break  # archives are newest-first; nothing older is relevant
        try:
            arch = _get(f"https://data.sec.gov/submissions/{file_info['name']}").json()
            results.extend(_extract_form4_entries(
                arch, issuer_cik, company_name, date_from, date_to,
            ))
        except Exception as exc:
            logger.warning("Archive fetch failed (%s): %s", file_info.get("name"), exc)

    logger.info("Found %d Form 4 filings for %s (%s)", len(results), company_name, issuer_cik)
    return results


def fetch_form4_xml_with_doc(cik: str, accession: str,
                              issuer_cik: str = None,
                              primary_doc: str = None) -> Optional[str]:
    """
    Like fetch_form4_xml but uses the known primary_doc filename first
    (available when coming from the submissions API).
    """
    accession_nodash = accession.replace("-", "")

    # Strategy 0: exact filename from submissions API (fastest, no guessing)
    if issuer_cik and primary_doc:
        url = (f"https://www.sec.gov/Archives/edgar/data/"
               f"{issuer_cik}/{accession_nodash}/{primary_doc}")
        try:
            resp = _get(url)
            if "<ownershipDocument" in resp.text:
                return resp.text
        except Exception:
            pass

    # Fall back to the standard XML fetch
    return fetch_form4_xml(cik, accession, issuer_cik=issuer_cik)
