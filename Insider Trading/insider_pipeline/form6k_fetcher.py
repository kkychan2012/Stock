"""
Fetches Form 6-K filings from SEC EDGAR for foreign private issuers.
Reuses the same rate-limited HTTP client as the Form 4 fetcher.
"""
import logging
from datetime import datetime
from typing import Optional

from sec_fetcher import _get

logger = logging.getLogger(__name__)


def fetch_form6k_by_company(issuer_cik: str, date_from: datetime,
                             date_to: datetime) -> list[dict]:
    """
    Get all Form 6-K filings for a company using the EDGAR submissions API.
    Returns list of filing dicts with cik, accession, filed_date, company_name,
    issuer_cik, primary_doc.
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
    results = _extract_6k_entries(
        data.get("filings", {}).get("recent", {}),
        issuer_cik, company_name, date_from, date_to,
    )

    for file_info in data.get("filings", {}).get("files", []):
        archive_end = file_info.get("date", "")
        if archive_end and datetime.strptime(archive_end, "%Y-%m-%d") < date_from:
            break
        try:
            arch = _get(f"https://data.sec.gov/submissions/{file_info['name']}").json()
            results.extend(_extract_6k_entries(
                arch, issuer_cik, company_name, date_from, date_to,
            ))
        except Exception as exc:
            logger.warning("Archive fetch failed (%s): %s", file_info.get("name"), exc)

    logger.info("Found %d Form 6-K filings for %s (%s)",
                len(results), company_name, issuer_cik)
    return results


def _extract_6k_entries(recent: dict, issuer_cik: str, company_name: str,
                         date_from: datetime, date_to: datetime) -> list[dict]:
    results = []
    for acc, form, date_str, primary_doc in zip(
        recent.get("accessionNumber", []),
        recent.get("form", []),
        recent.get("filingDate", []),
        recent.get("primaryDocument", []),
    ):
        if form not in ("6-K", "6-K/A"):
            continue
        try:
            filed_dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        if not (date_from <= filed_dt <= date_to):
            continue
        accession_nodash = acc.replace("-", "")
        results.append({
            "cik": accession_nodash[:10],
            "issuer_cik": issuer_cik.lstrip("0"),
            "accession": accession_nodash,
            "filed_date": date_str,
            "company_name": company_name,
            "primary_doc": primary_doc,
        })
    return results


def fetch_form6k_document(filing: dict) -> Optional[str]:
    """
    Download the primary document of a Form 6-K filing.
    Returns raw text (HTML or plain text), or None on failure.
    """
    issuer_cik = filing.get("issuer_cik", "")
    accession_nodash = filing.get("accession", "").replace("-", "")
    primary_doc = filing.get("primary_doc", "")

    # Strategy 1: exact primary_doc filename from submissions API
    if issuer_cik and primary_doc:
        url = (f"https://www.sec.gov/Archives/edgar/data/"
               f"{issuer_cik}/{accession_nodash}/{primary_doc}")
        try:
            resp = _get(url)
            if len(resp.text) > 200:
                return resp.text
        except Exception:
            pass

    # Strategy 2: fetch the filing index page and find the main document
    accession_fmt = (f"{accession_nodash[:10]}-{accession_nodash[10:12]}"
                     f"-{accession_nodash[12:]}") if len(accession_nodash) == 18 else accession_nodash
    for idx_cik in filter(None, [issuer_cik, filing.get("cik", "").lstrip("0")]):
        index_url = (f"https://www.sec.gov/Archives/edgar/data/"
                     f"{idx_cik}/{accession_nodash}/{accession_fmt}-index.htm")
        try:
            idx_resp = _get(index_url)
            import re
            # Look for .htm / .html document links (not the index itself)
            doc_paths = re.findall(
                r'href="(/Archives/edgar/data/[^"]+\.(?:htm|html|txt))"',
                idx_resp.text, re.IGNORECASE,
            )
            doc_paths = [p for p in doc_paths if "-index" not in p]
            for path in doc_paths[:3]:  # try first 3 candidate docs
                try:
                    doc_resp = _get("https://www.sec.gov" + path)
                    if len(doc_resp.text) > 200:
                        return doc_resp.text
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("6-K index fetch failed cik=%s: %s", idx_cik, exc)

    return None
