"""
congress_trades.py — Congress + executive-branch ("political") stock trade
disclosures, free and no API key required.

DATA SOURCE
-----------
The two community projects this feature was originally built around —
House Stock Watcher (housestockwatcher.com / its S3 bucket) and Senate Stock
Watcher (senatestockwatcher.com) — are both dead as of this writing: the
house-stock-watcher S3 bucket returns AccessDenied and the senatestockwatcher
domain no longer resolves at all.

In their place this uses kadoa-org/congress-trading-monitor, an actively
maintained (updated ~daily) open-source project that scrapes the same
official primary sources those two used to (the House Clerk's disclosure
site and the Senate's eFD system), PLUS Office of Government Ethics (OGE)
Form 278-T filings for the executive branch (President, VP, Cabinet, etc.),
and republishes a single merged JSON snapshot on GitHub — no auth, no API
key, no rate-limit headaches:
  https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/trades.json

The file is a rolling/recent snapshot maintained upstream (observed ~2 years
of transaction history, filings from the last couple of months), not a full
historical archive — same "recent activity" scope as the Insider tab. Not
every filer shows up in every snapshot — e.g. a Vice President with no
recently-filed PTR simply won't appear until one is scraped upstream.

Run standalone for a quick sanity-check:
  python congress_trades.py
"""

import re
from datetime import datetime, timedelta

import requests

from db_setup import get_connection, setup_database

TRADES_URL = (
    "https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor"
    "/main/public/data/trades.json"
)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-dashboard/1.0)"}

# All three "political" source_id values in the upstream feed.
_SOURCE_TO_CHAMBER = {
    "house_clerk":   "House",
    "senate_efd":    "Senate",
    "oge_executive": "Executive",
}
_CHAMBER_TO_SOURCE_LABEL = {
    "House":     "House-PTR",
    "Senate":    "Senate-PTR",
    "Executive": "OGE-278",
}
_CHAMBER_TO_DEFAULT_ROLE = {
    "House":     "Representative",
    "Senate":    "Senator",
    "Executive": "Executive Branch Official",
}

# Cluster detection — same rule of thumb as Insider tab's cluster_detector.py
# (Insider Trading/insider_pipeline/config.py: CLUSTER_WINDOW_DAYS=7,
# CLUSTER_MIN_INSIDERS=3), reimplemented in SQL rather than imported since
# that pipeline is a standalone subprocess-invoked package, not a library.
CLUSTER_WINDOW_DAYS      = 7
CLUSTER_MIN_POLITICIANS  = 3

_PLACEHOLDER_VALUES = {"", "--", "n/a", "na", "none", "null"}


def _emit(progress_cb, msg: str):
    if progress_cb:
        progress_cb(msg)
    else:
        print(msg, flush=True)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _clean(val):
    """"--"/""/None-ish placeholders → None; otherwise stripped string."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in _PLACEHOLDER_VALUES:
        return None
    return s


def _clean_date(val):
    """Normalise to YYYY-MM-DD; returns None if unparseable. The upstream
    feed already uses ISO dates, but this tolerates common alternates in
    case that changes upstream."""
    s = _clean(val)
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _normalise_type(transaction_type):
    t = (transaction_type or "").strip().lower()
    if t.startswith("purchase"):
        return "BUY"
    if t.startswith("sale"):
        return "SELL"
    if t.startswith("exchange"):
        return "EXCHANGE"
    return None


def _split_office(office):
    """Congress members: 'U.S. Representative · WA-01' -> ('U.S. Representative', 'WA-01').
    Executive branch: no '·' delimiter — the whole string ('President',
    'Secretary', ...) IS the role, there's no state/district.
    Falls back to (None, None) if the field is missing entirely."""
    office = _clean(office)
    if not office:
        return None, None
    if "·" not in office:
        return office, None
    role, _, state_or_district = office.partition("·")
    return role.strip() or None, state_or_district.strip() or None


def _normalise_record(raw: dict):
    """Map one upstream trade record to our congress_trades row shape.
    Returns None for unrecognised source_id or unusable rows."""
    chamber = _SOURCE_TO_CHAMBER.get(raw.get("source_id"))
    if chamber is None:
        return None

    tx_date = _clean_date(raw.get("transaction_date"))
    politician_name = _clean(raw.get("filer_name"))
    amount_range = _clean(raw.get("amount_range_label"))
    if not tx_date or not politician_name:
        return None  # can't dedupe or display meaningfully without these

    ticker = _clean(raw.get("ticker"))
    if ticker:
        ticker = ticker.upper()

    role, state_or_district = _split_office(raw.get("office"))
    if not role:
        role = f"U.S. {_CHAMBER_TO_DEFAULT_ROLE[chamber]}"
    if not state_or_district:
        # Executive-branch rows have no state/district; agency is the closest
        # useful equivalent (e.g. "White House Office").
        state_or_district = _clean(raw.get("state")) or _clean(raw.get("agency"))

    dedup_key = "|".join([
        politician_name, ticker or "", tx_date, amount_range or "",
    ])

    return {
        "tx_date":           tx_date,
        "filed_date":        _clean_date(raw.get("filing_date")),
        "ticker":            ticker,
        "company":           _clean(raw.get("asset_name")),
        "politician_name":   politician_name,
        "chamber":           chamber,
        "party":             _clean(raw.get("party")),
        "state_or_district": state_or_district,
        "role":              role,
        "type":              _normalise_type(raw.get("transaction_type")),
        "amount_range":      amount_range,
        "source":            _CHAMBER_TO_SOURCE_LABEL[chamber],
        "filing_url":        _clean(raw.get("doc_url")),
        "dedup_key":         dedup_key,
    }


# ---------------------------------------------------------------------------
# Cluster detection (SQL post-pass, run after every upsert)
# ---------------------------------------------------------------------------

def _flag_clusters(conn, emit):
    """Mark cluster_buy=1 on rows where >= CLUSTER_MIN_POLITICIANS distinct
    politicians traded the same ticker in the same direction (BUY/SELL/
    EXCHANGE) within CLUSTER_WINDOW_DAYS of each other. Recomputed in full
    on every fetch (cheap at this table's size) rather than tracked
    incrementally."""
    conn.execute("UPDATE congress_trades SET cluster_buy = 0")
    conn.execute(f"""
        UPDATE congress_trades SET cluster_buy = 1
        WHERE id IN (
            SELECT t1.id
            FROM congress_trades t1
            JOIN congress_trades t2
              ON t1.ticker = t2.ticker
             AND t1.type   = t2.type
             AND t2.politician_name != t1.politician_name
             AND ABS(JULIANDAY(t1.tx_date) - JULIANDAY(t2.tx_date)) <= {CLUSTER_WINDOW_DAYS}
            WHERE t1.ticker IS NOT NULL AND t1.type IS NOT NULL
            GROUP BY t1.id
            HAVING COUNT(DISTINCT t2.politician_name) >= {CLUSTER_MIN_POLITICIANS - 1}
        )
    """)
    n = conn.execute("SELECT COUNT(*) FROM congress_trades WHERE cluster_buy = 1").fetchone()[0]
    emit(f"Cluster detection: {n} rows flagged "
         f"({CLUSTER_MIN_POLITICIANS}+ politicians, same ticker+direction, "
         f"within {CLUSTER_WINDOW_DAYS} days).")


# ---------------------------------------------------------------------------
# Fetch + upsert
# ---------------------------------------------------------------------------

_INSERT_COLS = [
    "tx_date", "filed_date", "ticker", "company", "politician_name",
    "chamber", "party", "state_or_district", "role", "type",
    "amount_range", "source", "filing_url", "dedup_key",
]


def fetch_congress_trades(progress_cb=None) -> dict:
    """Download the latest snapshot, normalise, and upsert into
    congress_trades (INSERT OR IGNORE on dedup_key — matches the Insider
    pipeline's "skip duplicates" upsert style). Returns a summary dict."""
    emit = lambda msg: _emit(progress_cb, msg)
    setup_database()

    emit(f"Downloading {TRADES_URL} ...")
    resp = requests.get(TRADES_URL, headers=_HEADERS, timeout=60)
    resp.raise_for_status()
    raw_records = resp.json()
    emit(f"Downloaded {len(raw_records)} total records (House + Senate + executive branch).")

    rows = []
    skipped = 0
    for raw in raw_records:
        row = _normalise_record(raw)
        if row is None:
            skipped += 1
            continue
        rows.append(row)
    emit(f"{len(rows)} House/Senate/executive-branch records normalised, "
         f"{skipped} skipped (unrecognised source or unusable).")

    inserted = 0
    with get_connection() as conn:
        placeholders = ",".join(["?"] * len(_INSERT_COLS))
        sql = (
            f"INSERT OR IGNORE INTO congress_trades ({','.join(_INSERT_COLS)}) "
            f"VALUES ({placeholders})"
        )
        for row in rows:
            cur = conn.execute(sql, [row[c] for c in _INSERT_COLS])
            inserted += cur.rowcount
        conn.commit()
        emit(f"{inserted} new records saved to DB "
             f"({len(rows) - inserted} already present, skipped).")

        _flag_clusters(conn, emit)
        conn.commit()

    total = conn_total = None
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM congress_trades").fetchone()[0]

    emit(f"Done — {total} total records in congress_trades.")
    return {
        "downloaded": len(raw_records),
        "normalised": len(rows),
        "skipped":    skipped,
        "inserted":   inserted,
        "total":      total,
    }


if __name__ == "__main__":
    fetch_congress_trades()
