"""
universe.py — S&P 500 + Russell 1000 ticker universe, scraped from Wikipedia.

Provides the "tracked universe" used to compute real IBD-style RS Ratings
(see rs_calculator.py). Scraping is wrapped in try/except everywhere so a
Wikipedia structure change or network blip falls back to whatever was last
cached in ticker_universe rather than crashing the daily fetch pipeline.

Refresh cadence is WEEKLY (UNIVERSE_REFRESH_INTERVAL_DAYS) — index
reconstitution is infrequent (S&P 500 ad-hoc + quarterly; Russell 1000
annual each June), so re-scraping Wikipedia on every daily fetch is
unnecessary load.

Run standalone for a quick sanity-check:
  python universe.py
"""

import io
import re
from datetime import datetime, timedelta

import pandas as pd
import requests

from db_setup import get_connection, setup_database

# Wikipedia rejects requests with no User-Agent header (403) — pd.read_html()
# alone doesn't send one, so fetch the HTML ourselves first. The response text
# is wrapped in StringIO (not passed as a raw string) since pd.read_html can
# otherwise mis-detect a long HTML string as a filename and fail.
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-dashboard/1.0)"}


def _read_html_tables(url: str) -> list:
    resp = requests.get(url, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    return pd.read_html(io.StringIO(resp.text))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SP500_URL      = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
RUSSELL1000_URL = "https://en.wikipedia.org/wiki/Russell_1000_Index"

RS_UNIVERSE_SIZE_WARNING   = 200   # warn if active universe falls below this
UNIVERSE_REFRESH_INTERVAL_DAYS = 7  # weekly refresh cadence


def _emit(progress_cb, msg: str):
    if progress_cb:
        progress_cb(msg)
    else:
        print(msg, flush=True)


# ---------------------------------------------------------------------------
# Ticker normalisation
# ---------------------------------------------------------------------------

def _normalize_ticker(t) -> str | None:
    """yfinance-compatible ticker: '.' -> '-', stripped, uppercased."""
    if t is None:
        return None
    s = str(t).strip().upper()
    if not s or s == "NAN":
        return None
    return s.replace(".", "-")


# ---------------------------------------------------------------------------
# Wikipedia scraping
# ---------------------------------------------------------------------------

def _scrape_sp500() -> set[str]:
    try:
        tables = _read_html_tables(SP500_URL)
        df = tables[0]
        col = next((c for c in df.columns if str(c).strip().lower() == "symbol"), None)
        if col is None:
            return set()
        tickers = {_normalize_ticker(v) for v in df[col].tolist()}
        return {t for t in tickers if t}
    except Exception as exc:
        print(f"universe: S&P 500 scrape failed — {exc}", flush=True)
        return set()


def _scrape_russell1000() -> set[str]:
    try:
        tables = _read_html_tables(RUSSELL1000_URL)
        for df in tables:
            col = next(
                (c for c in df.columns
                 if re.search(r"ticker|symbol", str(c), re.IGNORECASE)),
                None,
            )
            if col is None:
                continue
            tickers = {_normalize_ticker(v) for v in df[col].tolist()}
            tickers = {t for t in tickers if t}
            if len(tickers) > 50:   # skip small unrelated tables on the page
                return tickers
        return set()
    except Exception as exc:
        print(f"universe: Russell 1000 scrape failed — {exc}", flush=True)
        return set()


def _cached_tickers(conn, source: str) -> set[str]:
    rows = conn.execute(
        "SELECT ticker FROM ticker_universe WHERE is_active = 1 "
        "AND (source = ? OR source = 'both')",
        (source,),
    ).fetchall()
    return {r["ticker"] for r in rows}


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

def refresh_universe(conn=None, progress_cb=None) -> dict:
    """Re-scrape S&P 500 + Russell 1000, union, upsert into ticker_universe.
    Falls back to the last cached set per source if a scrape fails.
    Returns a summary dict."""
    own_conn = conn is None
    if own_conn:
        setup_database()
        conn = get_connection()
    try:
        sp500 = _scrape_sp500()
        russell = _scrape_russell1000()

        if not sp500:
            sp500 = _cached_tickers(conn, "sp500")
            _emit(progress_cb, "universe: S&P 500 scrape failed — using cached tickers")
        if not russell:
            russell = _cached_tickers(conn, "russell1000")
            _emit(progress_cb, "universe: Russell 1000 scrape failed — using cached tickers")

        overlap = sp500 & russell
        all_tickers = sp500 | russell

        existing = {
            r["ticker"]: r["is_active"]
            for r in conn.execute("SELECT ticker, is_active FROM ticker_universe").fetchall()
        }
        today = datetime.now().strftime("%Y-%m-%d")

        upserts = []
        for t in all_tickers:
            if t in sp500 and t in russell:
                source = "both"
            elif t in sp500:
                source = "sp500"
            else:
                source = "russell1000"
            upserts.append((t, source, today))

        added = sum(1 for t, *_ in upserts if t not in existing or not existing[t])
        conn.executemany(
            """
            INSERT INTO ticker_universe (ticker, source, added_date, is_active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(ticker) DO UPDATE SET
                source = excluded.source,
                is_active = 1
            """,
            upserts,
        )

        removed = 0
        if all_tickers:
            currently_active = {
                r["ticker"] for r in conn.execute(
                    "SELECT ticker FROM ticker_universe WHERE is_active = 1"
                ).fetchall()
            }
            to_deactivate = currently_active - all_tickers
            if to_deactivate:
                conn.executemany(
                    "UPDATE ticker_universe SET is_active = 0 WHERE ticker = ?",
                    [(t,) for t in to_deactivate],
                )
                removed = len(to_deactivate)

        total_active = conn.execute(
            "SELECT COUNT(*) AS n FROM ticker_universe WHERE is_active = 1"
        ).fetchone()["n"]

        summary = {
            "added": added,
            "removed": removed,
            "total_active": total_active,
            "sp500_count": len(sp500),
            "russell1000_count": len(russell),
            "overlap_count": len(overlap),
        }

        conn.execute(
            """
            INSERT INTO universe_meta
                (id, last_refresh_date, sp500_count, russell1000_count, overlap_count, total_active)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_refresh_date = excluded.last_refresh_date,
                sp500_count       = excluded.sp500_count,
                russell1000_count = excluded.russell1000_count,
                overlap_count     = excluded.overlap_count,
                total_active      = excluded.total_active
            """,
            (today, len(sp500), len(russell), len(overlap), total_active),
        )
        if own_conn:
            conn.commit()

        _emit(
            progress_cb,
            f"universe: refreshed — +{added} added, -{removed} removed, "
            f"{total_active} active (S&P500={len(sp500)}, Russell1000={len(russell)}, "
            f"overlap={len(overlap)})",
        )
        if total_active < RS_UNIVERSE_SIZE_WARNING:
            _emit(
                progress_cb,
                f"WARNING: universe has only {total_active} tickers — RS Ratings will be "
                f"relative to tracked universe only, not full market",
            )
        return summary
    finally:
        if own_conn:
            conn.close()


def needs_refresh(conn=None) -> bool:
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        row = conn.execute(
            "SELECT last_refresh_date FROM universe_meta WHERE id = 1"
        ).fetchone()
        if not row or not row["last_refresh_date"]:
            return True
        last = datetime.strptime(row["last_refresh_date"], "%Y-%m-%d")
        return datetime.now() - last >= timedelta(days=UNIVERSE_REFRESH_INTERVAL_DAYS)
    finally:
        if own_conn:
            conn.close()


def get_active_universe(conn=None) -> list[str]:
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT ticker FROM ticker_universe WHERE is_active = 1 ORDER BY ticker"
        ).fetchall()
        tickers = [r["ticker"] for r in rows]
        if len(tickers) < RS_UNIVERSE_SIZE_WARNING:
            print(
                f"universe: active universe has only {len(tickers)} tickers — "
                f"RS Ratings will be relative to tracked universe only, not full market",
                flush=True,
            )
        return tickers
    finally:
        if own_conn:
            conn.close()


def get_universe_meta(conn=None) -> dict:
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM universe_meta WHERE id = 1").fetchone()
        return dict(row) if row else {
            "last_refresh_date": None, "sp500_count": 0, "russell1000_count": 0,
            "overlap_count": 0, "total_active": 0,
        }
    finally:
        if own_conn:
            conn.close()


if __name__ == "__main__":
    print("Refreshing ticker universe (S&P 500 + Russell 1000)…")
    summary = refresh_universe(progress_cb=print)
    print(summary)
