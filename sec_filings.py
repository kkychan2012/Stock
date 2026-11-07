"""
sec_filings.py — SEC Form 144 and Schedule 13D / 13G collector (whole market).

  Form 144   — notice of a PROPOSED sale by an insider/affiliate (filed on or
               before the sale; often ahead of the matching Form 4).
               -> table sec_form144 (one row per filing)
  13D / 13G  — someone owns > 5% of a company's stock. 13D = may seek to
               influence/control (activists, acquirers); 13G = passive holders
               (funds). /A = amendment (stake changed).
               -> table sec_13dg (one row per reporting person per filing)

Source: EDGAR daily form indexes
  https://www.sec.gov/Archives/edgar/daily-index/YYYY/QTRn/form.YYYYMMDD.idx
and each filing's structured XML (primary_doc.xml). Since 2023 (Form 144) and
Dec 2024 (13D/13G) these filings are XML; older free-text "SC 13D"/"SC 13G"
filings are not parsed (counted as skipped).

HTTP goes through the insider pipeline's sec_fetcher._get (SEC User-Agent from
its config.py, shared 10 req/s throttle, retries). Re-running is safe:
INSERT OR IGNORE on the accession number.

Called by nightly_collect.py. Standalone:
  python sec_filings.py 2026-11-04 [2026-11-05]
"""
import logging
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
_PIPELINE_DIR = os.path.join(ROOT, "Insider Trading", "insider_pipeline")
if _PIPELINE_DIR not in sys.path:
    sys.path.append(_PIPELINE_DIR)

import sec_fetcher  # noqa: E402  (insider pipeline: throttled SEC GET)
from db_setup import get_connection, setup_database  # noqa: E402

log = logging.getLogger("sec_filings")

FORM144_TYPES = {"144", "144/A"}
SCHED_TYPES = {"SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"}
LEGACY_SCHED_TYPES = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}

# ── helpers ──────────────────────────────────────────────────────────────────

_cik_to_ticker: dict = {}


def _ticker_for(cik) -> str | None:
    """Issuer CIK -> ticker via SEC company_tickers.json (loaded once)."""
    global _cik_to_ticker
    if not _cik_to_ticker:
        try:
            data = sec_fetcher._get("https://www.sec.gov/files/company_tickers.json").json()
            for v in data.values():
                _cik_to_ticker.setdefault(str(v["cik_str"]), v["ticker"].upper())
        except Exception as exc:
            log.warning("company_tickers.json failed: %s", exc)
            _cik_to_ticker = {"_": ""}
    if not cik:
        return None
    return _cik_to_ticker.get(str(cik).lstrip("0"))


def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _t(el, path):
    if el is None:
        return None
    node = el.find(path)
    if node is None or node.text is None:
        return None
    s = node.text.strip()
    return s or None


def _num(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def _date(s):
    """MM/DD/YYYY -> YYYY-MM-DD (passes ISO through)."""
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


def _daily_index(day: date) -> list[dict]:
    """Filings of the wanted form types on one day (empty on weekends/holidays)."""
    q = (day.month - 1) // 3 + 1
    url = (f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/QTR{q}/"
           f"form.{day:%Y%m%d}.idx")
    try:
        text = sec_fetcher._get(url).text
    except Exception as exc:
        msg = str(exc)
        if "404" in msg or "403" in msg:
            return []          # no index: weekend / holiday / not published yet
        raise
    wanted = FORM144_TYPES | SCHED_TYPES | LEGACY_SCHED_TYPES
    seen, out = set(), []
    for line in text.splitlines():
        m = re.match(r"^(.+?)\s{2,}(.+?)\s{2,}(\d+)\s+(\d{8})\s+(edgar/\S+\.txt)\s*$", line)
        if not m:
            continue
        form = m.group(1).strip()
        if form not in wanted:
            continue
        path = m.group(5)
        acc = os.path.basename(path)[:-4]           # 0001000275-26-000119
        if acc in seen:                             # listed once per party
            continue
        seen.add(acc)
        out.append({"form": form, "cik": m.group(3), "filed": m.group(4),
                    "path": path, "accession": acc})
    return out


def _fetch_xml(entry) -> ET.Element | None:
    """primary_doc.xml first (small); fall back to the full .txt submission."""
    acc_nodash = entry["accession"].replace("-", "")
    try:
        r = sec_fetcher._get(
            f"https://www.sec.gov/Archives/edgar/data/{entry['cik']}/{acc_nodash}/primary_doc.xml")
        return _strip_ns(ET.fromstring(r.content))
    except Exception:
        pass
    try:
        txt = sec_fetcher._get("https://www.sec.gov/Archives/" + entry["path"]).text
        m = re.search(r"<XML>\s*(<\?xml.*?)</XML>", txt, re.S)
        if m:
            return _strip_ns(ET.fromstring(m.group(1).encode("utf-8")))
    except Exception as exc:
        log.debug("fetch failed %s: %s", entry["accession"], exc)
    return None


def _filing_url(entry):
    acc = entry["accession"]
    return (f"https://www.sec.gov/Archives/edgar/data/{entry['cik']}/"
            f"{acc.replace('-', '')}/{acc}-index.htm")


# ── parsers ──────────────────────────────────────────────────────────────────

def parse_form144(root, entry) -> dict | None:
    fd = root.find("formData")
    if fd is None:
        return None
    issuer = fd.find("issuerInfo")
    sec = fd.find("securitiesInformation")
    tbs = fd.find("securitiesToBeSold")
    sig = fd.find("noticeSignature")
    rels = [e.text.strip() for e in issuer.findall("relationshipsToIssuer/relationshipToIssuer")
            if e.text] if issuer is not None else []
    plan_dates = [e.text.strip() for e in sig.findall("planAdoptionDates/planAdoptionDate")
                  if e.text] if sig is not None else []
    remarks = _t(fd, "remarks")
    issuer_cik = (_t(issuer, "issuerCik") or "").lstrip("0") or None
    return {
        "accession": entry["accession"],
        "form_type": entry["form"],
        "filed_date": _date(entry["filed"][:4] + "-" + entry["filed"][4:6] + "-" + entry["filed"][6:]),
        "issuer_cik": issuer_cik,
        "ticker": _ticker_for(issuer_cik),
        "company_name": _t(issuer, "issuerName"),
        "seller_name": _t(issuer, "nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold"),
        "relationship": ", ".join(rels) or None,
        "securities_class": _t(sec, "securitiesClassTitle"),
        "shares_to_sell": _num(_t(sec, "noOfUnitsSold")),
        "market_value": _num(_t(sec, "aggregateMarketValue")),
        "shares_outstanding": _num(_t(sec, "noOfUnitsOutstanding")),
        "approx_sale_date": _date(_t(sec, "approxSaleDate")),
        "exchange": _t(sec, "securitiesExchangeName"),
        "broker": _t(sec, "brokerOrMarketmakerDetails/name"),
        "acquired_how": _t(tbs, "natureOfAcquisitionTransaction"),
        "acquired_date": _date(_t(tbs, "acquiredDate")),
        "plan_10b51": 1 if plan_dates or (remarks and re.search(r"10b5-?1", remarks, re.I)) else 0,
        "plan_adoption_date": _date(plan_dates[0]) if plan_dates else None,
        "remarks": (remarks or "")[:1000] or None,
        "filing_url": _filing_url(entry),
    }


def parse_13dg(root, entry) -> list[dict]:
    fd = root.find("formData")
    if fd is None:
        return []
    is_d = "13D" in entry["form"]
    cph = fd.find("coverPageHeader")
    issuer = cph.find("issuerInfo") if cph is not None else None
    issuer_cik = ((_t(issuer, "issuerCIK") or _t(issuer, "issuerCik")) or "").lstrip("0") or None
    base = {
        "accession": entry["accession"],
        "form_type": entry["form"],
        "filed_date": f"{entry['filed'][:4]}-{entry['filed'][4:6]}-{entry['filed'][6:]}",
        "event_date": _date(_t(cph, "dateOfEvent") or _t(cph, "eventDateRequiresFilingThisStatement")),
        "issuer_cik": issuer_cik,
        "ticker": _ticker_for(issuer_cik),
        "company_name": _t(issuer, "issuerName"),
        "cusip": _t(issuer, "issuerCusips/issuerCusipNumber"),
        "securities_class": _t(cph, "securitiesClassTitle"),
        "rule": _t(cph, "designateRulesPursuantThisScheduleFiled/designateRulePursuantThisScheduleFiled"),
        "purpose": ((_t(fd, "items1To7/item4/transactionPurpose") or "")[:2000] or None) if is_d else None,
        "filing_url": _filing_url(entry),
    }
    persons = (fd.findall("reportingPersons/reportingPersonInfo") if is_d
               else fd.findall("coverPageHeaderReportingPersonDetails"))
    rows = []
    for i, p in enumerate(persons):
        if is_d:
            shares, pct = _t(p, "aggregateAmountOwned"), _t(p, "percentOfClass")
            pw = p
        else:
            shares = _t(p, "reportingPersonBeneficiallyOwnedAggregateNumberOfShares")
            pct = _t(p, "classPercent")
            pw = p.find("reportingPersonBeneficiallyOwnedNumberOfShares")
        rows.append({**base,
            "person_index": i,
            "filer_name": _t(p, "reportingPersonName"),
            "filer_cik": (_t(p, "reportingPersonCIK") or "").lstrip("0") or None,
            "person_type": _t(p, "typeOfReportingPerson"),
            "shares_owned": _num(shares),
            "percent_of_class": _num(pct),
            "sole_voting": _num(_t(pw, "soleVotingPower")),
            "shared_voting": _num(_t(pw, "sharedVotingPower")),
            "sole_dispositive": _num(_t(pw, "soleDispositivePower")),
            "shared_dispositive": _num(_t(pw, "sharedDispositivePower")),
        })
    return rows


# ── store ────────────────────────────────────────────────────────────────────

def _insert(conn, table, rows):
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = (f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) "
           f"VALUES ({','.join('?' * len(cols))})")
    return sum(conn.execute(sql, [r.get(c) for c in cols]).rowcount for r in rows)


def collect(date_from: date, date_to: date, progress=print) -> dict:
    """Fetch + store every Form 144 and 13D/13G filed date_from..date_to."""
    setup_database()
    stats = {"days": 0, "form144_new": 0, "sched_new_rows": 0,
             "already_stored": 0, "legacy_skipped": 0, "failed": 0}
    with get_connection() as conn:
        have144 = {r[0] for r in conn.execute("SELECT accession FROM sec_form144")}
        have13 = {r[0] for r in conn.execute("SELECT DISTINCT accession FROM sec_13dg")}
    day = date_from
    while day <= date_to:
        entries = _daily_index(day)
        if entries:
            stats["days"] += 1
        rows144, rows13 = [], []
        for e in entries:
            if e["form"] in LEGACY_SCHED_TYPES:
                stats["legacy_skipped"] += 1
                continue
            if e["accession"] in have144 or e["accession"] in have13:
                stats["already_stored"] += 1
                continue
            root = _fetch_xml(e)
            if root is None:
                stats["failed"] += 1
                continue
            try:
                if e["form"] in FORM144_TYPES:
                    r = parse_form144(root, e)
                    if r:
                        rows144.append(r)
                else:
                    rows13.extend(parse_13dg(root, e))
            except Exception as exc:
                stats["failed"] += 1
                log.warning("parse error %s %s: %s", e["form"], e["accession"], exc)
        with get_connection() as conn:          # save per day
            n144 = _insert(conn, "sec_form144", rows144)
            n13 = _insert(conn, "sec_13dg", rows13)
            conn.commit()
        stats["form144_new"] += n144
        stats["sched_new_rows"] += n13
        if entries:
            progress(f"{day}: {len(entries)} filings -> {n144} Form 144, {n13} 13D/G rows")
        day += timedelta(days=1)
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    d1 = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    d2 = datetime.strptime(sys.argv[2], "%Y-%m-%d").date() if len(sys.argv) > 2 else d1
    print(collect(d1, d2))
