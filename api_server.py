"""
Stock Dashboard API Server

Run:  python api_server.py
      python api_server.py --port 5001 --host 0.0.0.0

Endpoints
---------
GET  /                              Dashboard UI

-- Market data (read-only) --
GET  /api/holdings                  Holdings + live P/L from latest price
GET  /api/signals/ma200?days=30     price_gt_ma200 signals (last N days); &touch_ma=1 switches to "MA200 within Day Low-High"
GET  /api/signals/ma100?days=30     price_gt_ma100 signals (last N days)
GET  /api/signals/2xlow?multiplier=2.0  price_2x_low signals (close >= multiplier x all-time low)
GET  /api/signals/2xlow/first?multiplier=2.0  first-ever 2x-low hit per ticker, tracked + full universe
GET  /api/signals/ma1030?days=30    ma10_gt_ma30 signals  (last N days)
GET  /api/sold                      Sold positions
GET  /api/monitor                   Monitor list + latest price
GET  /api/prices                    Latest close for all tracked tickers

-- Extraction ticker list --
GET    /api/extraction/tickers      List all tickers
POST   /api/extraction/tickers      Add one ticker  {ticker, notes?}
DELETE /api/extraction/tickers/<t>  Remove one ticker
DELETE /api/extraction/tickers      Clear all tickers
POST   /api/extraction/upload       Bulk upload (CSV/XLSX file or JSON list)
GET    /api/extraction/download     Download ticker list as CSV

-- Fetch trigger --
POST /api/fetch                     Start background fetch  {period?}
GET  /api/fetch/status              Poll fetch progress

-- Congress / political trading disclosures (House + Senate PTRs) --
GET    /api/congress                List trades  ?ticker=&politician=&chamber=&party=&type=&from=&to=
GET    /api/congress/tickers        Distinct ticker list (for "has Congress activity" badges elsewhere)
GET    /api/congress/summary        Top BUY/SELL tickers for a month or month range  ?month=YYYY-MM or ?month_from=&month_to=&chamber=&party=
DELETE /api/congress/<id>           Delete one record
POST   /api/congress/fetch          Start background fetch (downloads latest snapshot, upserts)
GET    /api/congress/fetch/status   Poll fetch progress

Common query params:
  ?ticker=AAPL     Filter by ticker (market data endpoints)
  ?days=N          Signals: how many calendar days back
  ?latest=1        Signals: one most-recent row per ticker

-- Volume Profile (see volume_profile.py) --
GET  /api/volume-profile?ticker=&lookback=60&top_n=5   POC, top-N volume levels
     (support/resistance), pivot-based S/R "channels", and the levels confirmed by BOTH
"""

import argparse
import base64
import csv
import io
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, render_template, Response, send_file

from db_setup import get_connection, setup_database
from fetch_data import fetch_all, get_tickers_from_db
from scan_patterns import scan_all_patterns, scan_date_range, get_scan_results, get_scan_results_range, get_available_scan_dates
from backtest_engine import run_trading_simulation, calculate_metrics
import universe as universe_mod
from rs_calculator import RS_LINE_TREND_LOOKBACK
from congress_trades import fetch_congress_trades
from volume_profile import analyze as analyze_volume_profile, VP_LOOKBACK_DAYS, VP_TOP_N, VP_PIVOT_LOOKBACK_DAYS, VP_PIVOT_WINDOW
import surge_strategy

# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------

_DATE_FORMATS = [
    "%Y-%m-%d",   # 2024-01-31  canonical
    "%d/%m/%Y",   # 31/01/2024  AU/UK
    "%d/%m/%y",   # 31/01/24
    "%m/%d/%Y",   # 01/31/2024  US
    "%m/%d/%y",   # 01/31/24
    "%d-%m-%Y",   # 31-01-2024
    "%d-%m-%y",   # 31-01-24
    "%Y/%m/%d",   # 2024/01/31
    "%d.%m.%Y",   # 31.01.2024
    "%d.%m.%y",   # 31.01.24
    "%d %b %Y",   # 31 Jan 2024
    "%d %B %Y",   # 31 January 2024
    "%b %d, %Y",  # Jan 31, 2024
    "%B %d, %Y",  # January 31, 2024
]

_DATE_COLS = {"buy_date", "sell_date", "signal_date"}


def _normalise_date(s):
    """Return YYYY-MM-DD for any recognised date string, or None."""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Basic Auth  (enabled only when --user / DASH_USER is set)
# ---------------------------------------------------------------------------

_AUTH_USER: str | None = None
_AUTH_PASS: str        = ""


@app.before_request
def _require_auth():
    if _AUTH_USER is None:
        return                          # auth disabled — local-only mode
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
            if user == _AUTH_USER and pw == _AUTH_PASS:
                return                  # correct credentials
        except Exception:
            pass
    return Response(
        "Stock Dashboard — login required.",
        401,
        {"WWW-Authenticate": 'Basic realm="Stock Dashboard"'},
    )


# ---------------------------------------------------------------------------
# Fetch state (module-level, single-user local tool)
# ---------------------------------------------------------------------------

_fetch_state = {"running": False, "total": 0, "done": 0, "log": [], "error": None}
_fetch_lock  = threading.Lock()

_scan_state = {
    "running": False, "total": 0, "done": 0,
    "ticker": "", "error": None, "scan_date": None,
    "mode": "single", "from_date": None, "to_date": None,
}
_scan_lock = threading.Lock()

_insider_scan_state = {"running": False, "log": [], "error": None}
_insider_scan_lock  = threading.Lock()

_congress_fetch_state = {"running": False, "log": [], "error": None, "summary": None}
_congress_fetch_lock  = threading.Lock()

_market_cache      = {"data": None, "date": None}
_market_cache_lock = threading.Lock()

_INSIDER_PIPELINE = os.path.join(
    os.path.dirname(__file__), "Insider Trading", "insider_pipeline", "main.py"
)

VALID_PERIODS = {"1mo", "3mo", "6mo", "1y", "2y"}

# ---------------------------------------------------------------------------
# Dashboard UI
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("dashboard.html")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LATEST_PRICE_CTE = """
    WITH lp AS (
        SELECT ticker, MAX(date) AS max_date
        FROM stocks_daily
        WHERE close IS NOT NULL
        GROUP BY ticker
    )
"""

# Wide date ranges on Signals/RSI Signals can match 100k+ rows; rendering that many
# <tr> elements client-side freezes the browser tab. Cap what's returned and report
# the true match count so the frontend can tell the user to narrow their filter.
SIGNAL_ROW_CAP = 5000


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _ok(data):
    return jsonify({"count": len(data), "data": data})


def _ticker_filter(alias="h"):
    t = request.args.get("ticker", "").strip().upper()
    if t:
        return f" AND {alias}.ticker = ?", (t,)
    return "", ()


# ---------------------------------------------------------------------------
# GET /api/holdings
# ---------------------------------------------------------------------------

@app.get("/api/holdings")
def get_holdings():
    ticker_sql, ticker_params = _ticker_filter("h")
    sql = f"""
        {_LATEST_PRICE_CTE}
        SELECT
            h.id, h.ticker, h.stock_name, h.avg_buy_price, h.qty,
            h.buy_date, h.notes, h.added_at,
            sd.close                                                    AS current_price,
            sd.date                                                     AS price_date,
            sd.ma6, sd.ma10, sd.ma30, sd.ma50, sd.ma200, sd.rsi14,
            sd.high_30d, sd.low_30d, sd.vol_ma10,
            sd.pct_change                                               AS day_pct_change,
            sd.direction,
            ROUND((sd.close - h.avg_buy_price) * h.qty, 2)             AS pl_value,
            CASE
                WHEN h.avg_buy_price > 0
                THEN ROUND((sd.close - h.avg_buy_price) / h.avg_buy_price * 100, 2)
                ELSE NULL
            END                                                         AS pl_pct
        FROM holdings h
        LEFT JOIN lp ON h.ticker = lp.ticker
        LEFT JOIN stocks_daily sd ON sd.ticker = lp.ticker AND sd.date = lp.max_date
        WHERE 1=1 {ticker_sql}
        ORDER BY h.ticker
    """
    with get_connection() as conn:
        return _ok(_rows(conn, sql, ticker_params))


# ---------------------------------------------------------------------------
# GET /api/signals/ma200   GET /api/signals/ma1030
# ---------------------------------------------------------------------------

_SIGNAL_CONDS = {
    "price_gt_ma200": {
        "sig_where": "sig.close > sig.ma200 AND sig.ma200 IS NOT NULL",
        "sub_where": "sub.close > sub.ma200 AND sub.ma200 IS NOT NULL",
        "indicator":  "sig.ma200",
        "lookback":   "prev.close > prev.ma200 AND prev.ma200 IS NOT NULL",
    },
    "price_gt_ma100": {
        "sig_where": "sig.close > sig.ma100 AND sig.ma100 IS NOT NULL",
        "sub_where": "sub.close > sub.ma100 AND sub.ma100 IS NOT NULL",
        "indicator":  "sig.ma100",
        "lookback":   "prev.close > prev.ma100 AND prev.ma100 IS NOT NULL",
    },
    "ma10_gt_ma30": {
        "sig_where": "sig.ma10 > sig.ma30 AND sig.ma10 IS NOT NULL AND sig.ma30 IS NOT NULL",
        "sub_where": "sub.ma10 > sub.ma30 AND sub.ma10 IS NOT NULL AND sub.ma30 IS NOT NULL",
        "indicator":  "sig.ma10",
        "lookback":   "prev.ma10 > prev.ma30 AND prev.ma10 IS NOT NULL",
    },
    "price_lt_ma200": {
        "sig_where": "sig.close < sig.ma200 AND sig.ma200 IS NOT NULL",
        "sub_where": "sub.close < sub.ma200 AND sub.ma200 IS NOT NULL",
        "indicator":  "sig.ma200",
        "lookback":   "prev.close < prev.ma200 AND prev.ma200 IS NOT NULL",
    },
    "ma10_lt_ma30": {
        "sig_where": "sig.ma10 < sig.ma30 AND sig.ma10 IS NOT NULL AND sig.ma30 IS NOT NULL",
        "sub_where": "sub.ma10 < sub.ma30 AND sub.ma10 IS NOT NULL AND sub.ma30 IS NOT NULL",
        "indicator":  "sig.ma10",
        "lookback":   "prev.ma10 < prev.ma30 AND prev.ma10 IS NOT NULL",
    },
    # "Doubled from its low" — {mult} is substituted with a server-validated
    # float (see _get_signals), never raw user input, so this is safe.
    # low_alltime is a *point-in-time* running min of daily Low (see
    # fetch_data.py), so this fires on the exact day a ticker's close first
    # crosses N times whatever its low had been up to that day.
    "price_2x_low": {
        "sig_where": "sig.close >= {mult} * sig.low_alltime AND sig.low_alltime IS NOT NULL AND sig.low_alltime > 0",
        "sub_where": "sub.close >= {mult} * sub.low_alltime AND sub.low_alltime IS NOT NULL AND sub.low_alltime > 0",
        "indicator":  "sig.low_alltime",
        "lookback":   "prev.close >= {mult} * prev.low_alltime AND prev.low_alltime IS NOT NULL AND prev.low_alltime > 0",
    },
}

# "All" only aggregates the crossover/breakout conditions above — captured
# before rsi_signal is added below so RSI (a level, not a crossover event —
# true on most days a stock sits in a zone) doesn't flood that merged view.
_ALL_VIEW_TYPES = tuple(_SIGNAL_CONDS)

_VALID_RSI_ZONES = {"", "oversold", "overbought", "neutral"}


def _rsi_zone_cond(alias: str, zone: str) -> str:
    if zone == "oversold":
        return f"{alias}.rsi14 < 30"
    if zone == "overbought":
        return f"{alias}.rsi14 > 70"
    if zone == "neutral":
        return f"{alias}.rsi14 >= 30 AND {alias}.rsi14 <= 70"
    return "1=1"  # "" == All — no extra restriction beyond rsi14 being present


# Real condition strings aren't used (see the signal_type == "rsi_signal"
# branch below, which builds zone-aware conditions per alias instead) —
# this entry exists so "rsi_signal" is a recognised key for direct lookups.
_SIGNAL_CONDS["rsi_signal"] = {
    "sig_where": "sig.rsi14 IS NOT NULL",
    "sub_where": "sub.rsi14 IS NOT NULL",
    "indicator":  "sig.rsi14",
    "lookback":   "prev.rsi14 IS NOT NULL",
}

_DEFAULT_MULTIPLIER = 2.0


def _fetch_signal_rows(signal_type, date_from, date_to, latest_only, lookback, ticker_sql, ticker_params,
                        multiplier=_DEFAULT_MULTIPLIER, zone="", touch_ma=False):
    cond = _SIGNAL_CONDS[signal_type]
    if signal_type == "rsi_signal":
        sig_where_sig = f"sig.rsi14 IS NOT NULL AND {_rsi_zone_cond('sig', zone)}"
        sig_where_sub = f"sub.rsi14 IS NOT NULL AND {_rsi_zone_cond('sub', zone)}"
        lookback_cond = f"prev.rsi14 IS NOT NULL AND {_rsi_zone_cond('prev', zone)}"
        indicator_col = "sig.rsi14"
    elif touch_ma and signal_type in ("price_gt_ma200", "price_lt_ma200"):
        # "MA200 within Day Low–Day High" — the MA200 line sat somewhere inside
        # the day's traded range, i.e. price crossed/tested it at some point
        # that day, independent of where it closed. Same condition regardless
        # of which of the two MA200 buttons is active (bull vs bear) — both
        # describe "did today's range touch MA200", not a directional cross.
        sig_where_sig = "sig.low <= sig.ma200 AND sig.ma200 <= sig.high AND sig.ma200 IS NOT NULL"
        sig_where_sub = "sub.low <= sub.ma200 AND sub.ma200 <= sub.high AND sub.ma200 IS NOT NULL"
        lookback_cond = "prev.low <= prev.ma200 AND prev.ma200 <= prev.high AND prev.ma200 IS NOT NULL"
        indicator_col = "sig.ma200"
    else:
        # .format(mult=...) is a no-op for conditions with no "{mult}" placeholder.
        sig_where_sig = cond["sig_where"].format(mult=multiplier)
        sig_where_sub = cond["sub_where"].format(mult=multiplier)
        indicator_col = cond["indicator"]
        lookback_cond = cond["lookback"].format(mult=multiplier)

    if latest_only:
        dedup_cte = f"""
            , dedup AS (
                SELECT sub.ticker, MAX(sub.date) AS max_sig_date
                FROM stocks_daily sub
                WHERE {sig_where_sub}
                  AND sub.date >= ? AND sub.date <= ?
                GROUP BY sub.ticker
            )
        """
        dedup_join   = "JOIN dedup ON sig.ticker = dedup.ticker AND sig.date = dedup.max_sig_date"
        dedup_params = (date_from, date_to)
    else:
        dedup_cte, dedup_join, dedup_params = "", "", ()

    if lookback > 0:
        lookback_clause = f"""
            AND NOT EXISTS (
                SELECT 1 FROM stocks_daily prev
                WHERE prev.ticker = sig.ticker
                  AND prev.date >= (
                      SELECT MIN(bd.date) FROM (
                          SELECT date FROM stocks_daily
                          WHERE ticker = sig.ticker
                            AND date   < sig.date
                          ORDER BY date DESC
                          LIMIT ?
                      ) bd
                  )
                  AND prev.date < sig.date
                  AND prev.close IS NOT NULL
                  AND {lookback_cond}
            )
        """
        lookback_params = (lookback,)
    else:
        lookback_clause, lookback_params = "", ()

    count_sql = f"""
        {_LATEST_PRICE_CTE}
        {dedup_cte}
        SELECT COUNT(*) AS cnt
        FROM stocks_daily sig
        {dedup_join}
        WHERE {sig_where_sig}
          AND sig.date >= ? AND sig.date <= ?
          {lookback_clause}
          {ticker_sql}
    """
    sql = f"""
        {_LATEST_PRICE_CTE}
        {dedup_cte}
        SELECT
            '{signal_type}'          AS signal_type,
            sig.ticker,
            sig.date                 AS signal_date,
            sig.close                AS signal_close,
            sig.low                  AS day_low,
            sig.high                 AS day_high,
            {indicator_col}          AS indicator_value,
            sig.ma6, sig.ma10, sig.ma30, sig.ma50, sig.ma100, sig.ma150, sig.ma200, sig.rsi14,
            sig.high_30d, sig.low_30d, sig.high_52wk, sig.low_52wk, sig.low_alltime, sig.low_alltime_date,
            COALESCE(rr.rs_rating, sig.rs_rank) AS rs_rank,
            rr.rs_leader              AS rs_leader,
            sig.c1, sig.c2, sig.c3, sig.c4, sig.c5, sig.c6, sig.c7, sig.c8, sig.trend_score,
            cur.close                AS current_price,
            cur.date                 AS current_price_date,
            CASE
                WHEN sig.close > 0 AND cur.close IS NOT NULL
                THEN ROUND((cur.close - sig.close) / sig.close * 100, 2)
                ELSE NULL
            END                      AS day_pct_change,
            cur.direction
        FROM stocks_daily sig
        {dedup_join}
        LEFT JOIN lp ON sig.ticker = lp.ticker
        LEFT JOIN stocks_daily cur
               ON cur.ticker = lp.ticker AND cur.date = lp.max_date
        LEFT JOIN rs_ratings rr
               ON rr.ticker = sig.ticker AND rr.date = sig.date
        WHERE {sig_where_sig}
          AND sig.date >= ? AND sig.date <= ?
          {lookback_clause}
          {ticker_sql}
        ORDER BY sig.date DESC, sig.ticker
        LIMIT ?
    """
    params = dedup_params + (date_from, date_to) + lookback_params + ticker_params
    with get_connection() as conn:
        total = conn.execute(count_sql, params).fetchone()[0]
        rows  = _rows(conn, sql, params + (SIGNAL_ROW_CAP,))
    return rows, total


def _get_signals(signal_type: str):
    today = datetime.now().strftime("%Y-%m-%d")
    ago30 = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    date_from   = request.args.get("from",     ago30)
    date_to     = request.args.get("to",       today)
    latest_only = request.args.get("latest",   "0") in ("1", "true", "yes")
    lookback    = request.args.get("lookback", 0, type=int)
    ticker_sql, ticker_params = _ticker_filter("sig")

    # Only meaningful for price_2x_low, but harmless to parse unconditionally.
    try:
        multiplier = float(request.args.get("multiplier", _DEFAULT_MULTIPLIER))
    except (TypeError, ValueError):
        multiplier = _DEFAULT_MULTIPLIER
    if not (1.001 <= multiplier <= 1000):
        multiplier = _DEFAULT_MULTIPLIER

    # Only meaningful for rsi_signal, but harmless to parse unconditionally.
    zone = request.args.get("zone", "").strip().lower()
    if zone not in _VALID_RSI_ZONES:
        zone = ""

    # Only meaningful for price_gt_ma200/price_lt_ma200, but harmless elsewhere.
    touch_ma = request.args.get("touch_ma", "0") in ("1", "true", "yes")

    if signal_type == "all":
        rows, total = [], 0
        for st in _ALL_VIEW_TYPES:
            r, t = _fetch_signal_rows(st, date_from, date_to, latest_only, lookback, ticker_sql, ticker_params, multiplier, zone, touch_ma)
            rows += r
            total += t
        rows.sort(key=lambda r: (r.get("signal_date") or ""), reverse=True)
        rows = rows[:SIGNAL_ROW_CAP]
    else:
        rows, total = _fetch_signal_rows(signal_type, date_from, date_to, latest_only, lookback, ticker_sql, ticker_params, multiplier, zone, touch_ma)

    return jsonify({"count": len(rows), "total": total, "truncated": total > len(rows), "data": rows})


@app.get("/api/signals/ma200")
def get_signals_ma200():
    return _get_signals("price_gt_ma200")


@app.get("/api/signals/ma100")
def get_signals_ma100():
    return _get_signals("price_gt_ma100")


@app.get("/api/signals/2xlow")
def get_signals_2x_low():
    return _get_signals("price_2x_low")


@app.get("/api/signals/2xlow/first")
def get_signals_2x_low_first():
    """One row per ticker — the very FIRST date its close ever reached
    >= multiplier x its all-time-low-so-far (low_alltime), across BOTH the
    tracked Ticker List (stocks_daily) and the full S&P 500 + Russell 1000
    universe (universe_prices; tracked tickers excluded there to avoid
    double-counting — same "tracked wins" pattern as /api/data/summary).
    Unlike /api/signals/2xlow this ignores date range / latest-only /
    lookback — it always looks across each ticker's whole stored history
    and returns exactly one (first) hit per ticker."""
    try:
        multiplier = float(request.args.get("multiplier", _DEFAULT_MULTIPLIER))
    except (TypeError, ValueError):
        multiplier = _DEFAULT_MULTIPLIER
    if not (1.001 <= multiplier <= 1000):
        multiplier = _DEFAULT_MULTIPLIER
    ticker_sql, ticker_params = _ticker_filter("s")

    sql = f"""
        WITH src AS (
            SELECT ticker, date, close, low AS day_low, high AS day_high, low_alltime, low_alltime_date,
                   ma6, ma10, ma30, ma50, ma100, ma150, ma200, rsi14,
                   high_30d, low_30d, high_52wk, low_52wk,
                   c1, c2, c3, c4, c5, c6, c7, c8, trend_score,
                   1 AS tracked
            FROM stocks_daily
            WHERE low_alltime IS NOT NULL AND low_alltime > 0
            UNION ALL
            SELECT ticker, date, close, low AS day_low, high AS day_high, low_alltime, low_alltime_date,
                   ma6, ma10, ma30, ma50, NULL AS ma100, ma150, ma200, rsi14,
                   high_30d, low_30d, high_52wk, low_52wk,
                   c1, c2, c3, c4, c5, c6, c7, c8, trend_score,
                   0 AS tracked
            FROM universe_prices
            WHERE low_alltime IS NOT NULL AND low_alltime > 0
              AND ticker IN (SELECT ticker FROM ticker_universe WHERE is_active = 1)
              AND ticker NOT IN (SELECT ticker FROM extraction_tickers)
        ),
        hits AS (
            SELECT ticker, MIN(date) AS first_date
            FROM src
            WHERE close >= ? * low_alltime
            GROUP BY ticker
        ),
        cur_tracked AS (
            SELECT ticker, MAX(date) AS max_date FROM stocks_daily
            WHERE close IS NOT NULL GROUP BY ticker
        ),
        cur_universe AS (
            SELECT ticker, MAX(date) AS max_date FROM universe_prices
            WHERE close IS NOT NULL
              AND ticker IN (SELECT ticker FROM ticker_universe WHERE is_active = 1)
              AND ticker NOT IN (SELECT ticker FROM extraction_tickers)
            GROUP BY ticker
        ),
        cur AS (
            SELECT ct.ticker, sd.close, sd.date FROM cur_tracked ct
            JOIN stocks_daily sd ON sd.ticker = ct.ticker AND sd.date = ct.max_date
            UNION ALL
            SELECT cu.ticker, up.close, up.date FROM cur_universe cu
            JOIN universe_prices up ON up.ticker = cu.ticker AND up.date = cu.max_date
        )
        SELECT
            s.ticker,
            h.first_date              AS signal_date,
            s.close                   AS signal_close,
            s.day_low, s.day_high,
            s.low_alltime, s.low_alltime_date,
            s.ma6, s.ma10, s.ma30, s.ma50, s.ma100, s.ma150, s.ma200, s.rsi14,
            s.high_30d, s.low_30d, s.high_52wk, s.low_52wk,
            s.c1, s.c2, s.c3, s.c4, s.c5, s.c6, s.c7, s.c8, s.trend_score,
            s.tracked,
            COALESCE(rr.rs_rating, NULL) AS rs_rank,
            rr.rs_leader               AS rs_leader,
            cur.close                 AS current_price,
            cur.date                  AS current_price_date,
            CASE
                WHEN s.close > 0 AND cur.close IS NOT NULL
                THEN ROUND((cur.close - s.close) / s.close * 100, 2)
                ELSE NULL
            END                       AS day_pct_change
        FROM src s
        JOIN hits h ON h.ticker = s.ticker AND h.first_date = s.date
        LEFT JOIN rs_ratings rr ON rr.ticker = s.ticker AND rr.date = s.date
        LEFT JOIN cur ON cur.ticker = s.ticker
        WHERE 1=1
        {ticker_sql}
        ORDER BY h.first_date DESC, s.ticker
        LIMIT 2000
    """
    params = (multiplier,) + ticker_params
    with get_connection() as conn:
        rows = _rows(conn, sql, params)
    return jsonify({"count": len(rows), "data": rows})


@app.get("/api/signals/ma1030")
def get_signals_ma1030():
    return _get_signals("ma10_gt_ma30")


@app.get("/api/signals/ma200bear")
def get_signals_ma200bear():
    return _get_signals("price_lt_ma200")


@app.get("/api/signals/ma1030bear")
def get_signals_ma1030bear():
    return _get_signals("ma10_lt_ma30")


@app.get("/api/signals/all")
def get_signals_all():
    return _get_signals("all")


# ---------------------------------------------------------------------------
# GET /api/signals/rsi — merged into the general signal engine above
# (rsi_signal in _SIGNAL_CONDS); ?zone=oversold|overbought|neutral (omit
# for "All"). Kept as its own route/URL since the Signals tab UI calls it
# directly, same as ma200/ma100/2xlow.
# ---------------------------------------------------------------------------

@app.get("/api/signals/rsi")
def get_rsi_signals():
    return _get_signals("rsi_signal")


# ---------------------------------------------------------------------------
# GET /api/sold
# ---------------------------------------------------------------------------

@app.get("/api/sold")
def get_sold():
    ticker_sql, ticker_params = _ticker_filter("s")
    sql = f"""
        SELECT
            s.id, s.ticker, s.stock_name,
            s.avg_buy_price, s.qty, s.buy_date,
            s.sell_price, s.sell_date,
            s.pl_value, s.notes, s.created_at,
            CASE
                WHEN s.avg_buy_price > 0
                THEN ROUND((s.sell_price - s.avg_buy_price) / s.avg_buy_price * 100, 2)
                ELSE NULL
            END AS pl_pct
        FROM sold s
        WHERE 1=1 {ticker_sql}
        ORDER BY s.sell_date DESC, s.ticker
    """
    with get_connection() as conn:
        return _ok(_rows(conn, sql, ticker_params))


# ---------------------------------------------------------------------------
# Holdings CRUD + upload/download
# ---------------------------------------------------------------------------

def _parse_record_body(body, required):
    """Extract and validate fields from a JSON request body."""
    out = {}
    errors = []
    for field in required:
        val = body.get(field)
        if val is None or str(val).strip() == "":
            errors.append(f"{field} is required")
        else:
            out[field] = val
    if errors:
        return None, errors
    # optional fields
    for field in ("stock_name", "buy_date", "notes", "sell_date"):
        if field in body:
            v = body[field] or None
            out[field] = _normalise_date(v) if field in _DATE_COLS else v
    return out, []


@app.post("/api/holdings")
def add_holding():
    body = request.get_json(silent=True) or {}
    body["ticker"] = (body.get("ticker") or "").strip().upper()
    data, errors = _parse_record_body(body, ["ticker", "avg_buy_price", "qty"])
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400
    with get_connection() as conn:
        try:
            conn.execute("""
                INSERT INTO holdings (ticker, stock_name, avg_buy_price, qty, buy_date, notes)
                VALUES (:ticker, :stock_name, :avg_buy_price, :qty, :buy_date, :notes)
                ON CONFLICT(ticker) DO UPDATE SET
                    stock_name    = excluded.stock_name,
                    avg_buy_price = excluded.avg_buy_price,
                    qty           = excluded.qty,
                    buy_date      = excluded.buy_date,
                    notes         = excluded.notes
            """, {**{"stock_name": None, "buy_date": None, "notes": None}, **data})
        except Exception as e:
            return jsonify({"error": str(e)}), 400
    return jsonify({"saved": data["ticker"]}), 201


@app.put("/api/holdings/<int:rec_id>")
def update_holding(rec_id):
    body = request.get_json(silent=True) or {}
    if "ticker" in body:
        body["ticker"] = body["ticker"].strip().upper()
    data, errors = _parse_record_body(body, ["ticker", "avg_buy_price", "qty"])
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400
    with get_connection() as conn:
        cur = conn.execute("""
            UPDATE holdings
            SET ticker        = :ticker,
                stock_name    = :stock_name,
                avg_buy_price = :avg_buy_price,
                qty           = :qty,
                buy_date      = :buy_date,
                notes         = :notes
            WHERE id = :id
        """, {**{"stock_name": None, "buy_date": None, "notes": None}, **data, "id": rec_id})
    if cur.rowcount == 0:
        return jsonify({"error": "record not found"}), 404
    return jsonify({"updated": rec_id})


@app.post("/api/holdings/<int:holding_id>/sell")
def sell_holding(holding_id):
    body = request.get_json(silent=True) or {}
    try:
        sell_qty   = float(body.get("sell_qty",   0))
        sell_price = float(body.get("sell_price", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "sell_qty and sell_price must be numbers"}), 400

    sell_date = _normalise_date((body.get("sell_date") or "").strip() or None)
    notes     = (body.get("notes") or "").strip() or None

    if sell_qty <= 0:
        return jsonify({"error": "sell_qty must be greater than 0"}), 400
    if sell_price <= 0:
        return jsonify({"error": "sell_price must be greater than 0"}), 400

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM holdings WHERE id = ?", (holding_id,)).fetchone()
        if not row:
            return jsonify({"error": "holding not found"}), 404
        holding = dict(row)

        if sell_qty > holding["qty"]:
            return jsonify({"error": f"sell_qty ({sell_qty}) exceeds holding qty ({holding['qty']})"}), 400

        pl_value = round((sell_price - (holding["avg_buy_price"] or 0)) * sell_qty, 2)

        conn.execute("""
            INSERT INTO sold (ticker, stock_name, avg_buy_price, qty, buy_date,
                              sell_price, sell_date, pl_value, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (holding["ticker"], holding["stock_name"], holding["avg_buy_price"],
              sell_qty, holding["buy_date"], sell_price, sell_date, pl_value, notes))

        remaining = holding["qty"] - sell_qty
        if remaining <= 0:
            conn.execute("DELETE FROM holdings WHERE id = ?", (holding_id,))
        else:
            conn.execute("UPDATE holdings SET qty = ? WHERE id = ?", (remaining, holding_id))

    return jsonify({"sold": holding["ticker"], "remaining_qty": max(0, remaining)}), 201


@app.delete("/api/holdings/<int:rec_id>")
def delete_holding(rec_id):
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM holdings WHERE id = ?", (rec_id,))
    if cur.rowcount == 0:
        return jsonify({"error": "record not found"}), 404
    return jsonify({"deleted": rec_id})


@app.post("/api/holdings/upload")
def upload_holdings():
    return _upload_table(
        request,
        table="holdings",
        columns=["ticker", "stock_name", "avg_buy_price", "qty", "buy_date", "notes"],
        upsert=True,
    )


@app.get("/api/holdings/download")
def download_holdings():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT ticker, stock_name, avg_buy_price, qty, buy_date, notes FROM holdings ORDER BY ticker"
        ).fetchall()
    return _csv_response(rows, "holdings.csv")


# ---------------------------------------------------------------------------
# Sold CRUD + upload/download
# ---------------------------------------------------------------------------

@app.post("/api/sold")
def add_sold():
    body = request.get_json(silent=True) or {}
    body["ticker"] = (body.get("ticker") or "").strip().upper()
    data, errors = _parse_record_body(
        body, ["ticker", "avg_buy_price", "qty", "sell_price", "sell_date"]
    )
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400
    # Auto-calculate pl_value if not provided
    if "pl_value" not in data or data.get("pl_value") is None:
        try:
            data["pl_value"] = round(
                (float(data["sell_price"]) - float(data["avg_buy_price"])) * float(data["qty"]), 2
            )
        except (TypeError, ValueError):
            data["pl_value"] = None
    with get_connection() as conn:
        cur = conn.execute("""
            INSERT INTO sold (ticker, stock_name, avg_buy_price, qty, buy_date,
                              sell_price, sell_date, pl_value, notes)
            VALUES (:ticker, :stock_name, :avg_buy_price, :qty, :buy_date,
                    :sell_price, :sell_date, :pl_value, :notes)
        """, {**{"stock_name": None, "buy_date": None, "notes": None, "pl_value": None}, **data})
        new_id = cur.lastrowid
    return jsonify({"saved": new_id}), 201


@app.put("/api/sold/<int:rec_id>")
def update_sold(rec_id):
    body = request.get_json(silent=True) or {}
    if "ticker" in body:
        body["ticker"] = body["ticker"].strip().upper()
    data, errors = _parse_record_body(
        body, ["ticker", "avg_buy_price", "qty", "sell_price", "sell_date"]
    )
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400
    try:
        data["pl_value"] = round(
            (float(data["sell_price"]) - float(data["avg_buy_price"])) * float(data["qty"]), 2
        )
    except (TypeError, ValueError):
        data["pl_value"] = None
    with get_connection() as conn:
        cur = conn.execute("""
            UPDATE sold
            SET ticker        = :ticker,
                stock_name    = :stock_name,
                avg_buy_price = :avg_buy_price,
                qty           = :qty,
                buy_date      = :buy_date,
                sell_price    = :sell_price,
                sell_date     = :sell_date,
                pl_value      = :pl_value,
                notes         = :notes
            WHERE id = :id
        """, {**{"stock_name": None, "buy_date": None, "notes": None}, **data, "id": rec_id})
    if cur.rowcount == 0:
        return jsonify({"error": "record not found"}), 404
    return jsonify({"updated": rec_id})


@app.delete("/api/sold/<int:rec_id>")
def delete_sold(rec_id):
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM sold WHERE id = ?", (rec_id,))
    if cur.rowcount == 0:
        return jsonify({"error": "record not found"}), 404
    return jsonify({"deleted": rec_id})


@app.post("/api/sold/upload")
def upload_sold():
    return _upload_table(
        request,
        table="sold",
        columns=["ticker", "stock_name", "avg_buy_price", "qty", "buy_date",
                 "sell_price", "sell_date", "pl_value", "notes"],
        upsert=False,
    )


@app.get("/api/sold/download")
def download_sold():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ticker, stock_name, avg_buy_price, qty, buy_date,
                   sell_price, sell_date, pl_value, notes
            FROM sold ORDER BY sell_date DESC, ticker
        """).fetchall()
    return _csv_response(rows, "sold.csv")


# ---------------------------------------------------------------------------
# Shared upload / download helpers
# ---------------------------------------------------------------------------

def _read_upload_rows(req):
    """Parse CSV or XLSX from a multipart file upload. Returns list of dicts."""
    if "file" not in req.files:
        return None, "No file in request"
    f    = req.files["file"]
    name = (f.filename or "").lower()
    if name.endswith(".csv"):
        text   = f.read().decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        return [row for row in reader], None
    elif name.endswith((".xlsx", ".xls")):
        import openpyxl
        wb  = openpyxl.load_workbook(io.BytesIO(f.read()), read_only=True, data_only=True)
        ws  = wb.active
        headers = None
        rows = []
        for row in ws.iter_rows(values_only=True):
            if headers is None:
                headers = [str(c).strip().lower() if c else "" for c in row]
            else:
                rows.append({headers[i]: (str(v).strip() if v is not None else "") for i, v in enumerate(row)})
        wb.close()
        return rows, None
    else:
        return None, "Only .csv and .xlsx files are supported"


def _upload_table(req, table, columns, upsert):
    rows, err = _read_upload_rows(req)
    if err:
        return jsonify({"error": err}), 400
    if not rows:
        return jsonify({"error": "No data rows found in file"}), 400

    added = skipped = 0
    with get_connection() as conn:
        for row in rows:
            # Normalise keys to lowercase
            row = {k.lower().strip(): v for k, v in row.items()}
            ticker = (row.get("ticker") or "").strip().upper()
            if not ticker:
                skipped += 1
                continue

            vals = {"ticker": ticker}
            for col in columns:
                if col == "ticker":
                    continue
                v = row.get(col, "")
                vals[col] = v.strip() if isinstance(v, str) else v or None
                if vals[col] == "":
                    vals[col] = None
                if col in _DATE_COLS and vals[col]:
                    vals[col] = _normalise_date(vals[col])

            # Auto-calc pl_value for sold if missing
            if table == "sold" and not vals.get("pl_value"):
                try:
                    vals["pl_value"] = round(
                        (float(vals["sell_price"]) - float(vals["avg_buy_price"])) * float(vals["qty"]), 2
                    )
                except (TypeError, ValueError):
                    vals["pl_value"] = None

            col_list = ", ".join(columns)
            placeholders = ", ".join(f":{c}" for c in columns)

            if upsert:
                update_set = ", ".join(
                    f"{c} = excluded.{c}" for c in columns if c != "ticker"
                )
                sql = f"""
                    INSERT INTO {table} ({col_list}) VALUES ({placeholders})
                    ON CONFLICT(ticker) DO UPDATE SET {update_set}
                """
            else:
                sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"

            try:
                conn.execute(sql, vals)
                added += 1
            except Exception:
                skipped += 1

    return jsonify({"added": added, "skipped": skipped})


def _csv_response(rows, filename):
    if not rows:
        buf = io.StringIO()
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={filename}"})
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(rows[0].keys())
    for r in rows:
        writer.writerow([r[k] if r[k] is not None else "" for k in r.keys()])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


# ---------------------------------------------------------------------------
# GET /api/monitor
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# GET /api/watchlists  — watchlist management
# ---------------------------------------------------------------------------

@app.get("/api/watchlists")
def get_watchlists():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT w.id, w.name, COUNT(m.id) AS ticker_count
            FROM watchlists w
            LEFT JOIN monitor_list m ON m.watchlist_id = w.id
            GROUP BY w.id, w.name
            ORDER BY w.id
        """).fetchall()
    return _ok([dict(r) for r in rows])


@app.post("/api/watchlists")
def create_watchlist():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    with get_connection() as conn:
        try:
            cur = conn.execute("INSERT INTO watchlists (name) VALUES (?)", (name,))
            return jsonify({"id": cur.lastrowid, "name": name}), 201
        except Exception:
            return jsonify({"error": f"Watchlist '{name}' already exists"}), 409


@app.put("/api/watchlists/<int:wl_id>")
def rename_watchlist(wl_id):
    if wl_id == 1:
        return jsonify({"error": "Cannot rename the Default watchlist"}), 400
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    with get_connection() as conn:
        cur = conn.execute("UPDATE watchlists SET name=? WHERE id=?", (name, wl_id))
    if cur.rowcount == 0:
        return jsonify({"error": "watchlist not found"}), 404
    return jsonify({"updated": wl_id, "name": name})


@app.delete("/api/watchlists/<int:wl_id>")
def delete_watchlist(wl_id):
    if wl_id == 1:
        return jsonify({"error": "Cannot delete the Default watchlist"}), 400
    with get_connection() as conn:
        conn.execute("DELETE FROM monitor_list WHERE watchlist_id = ?", (wl_id,))
        cur = conn.execute("DELETE FROM watchlists WHERE id = ?", (wl_id,))
    if cur.rowcount == 0:
        return jsonify({"error": "watchlist not found"}), 404
    return jsonify({"deleted": wl_id})


# ---------------------------------------------------------------------------
# GET /api/monitor
# ---------------------------------------------------------------------------

@app.get("/api/monitor")
def get_monitor():
    ticker_sql, ticker_params = _ticker_filter("m")
    watchlist_id = request.args.get("watchlist", type=int)
    wl_clause  = "AND m.watchlist_id = ?" if watchlist_id else ""
    wl_params  = (watchlist_id,) if watchlist_id else ()
    sql = f"""
        {_LATEST_PRICE_CTE}
        SELECT
            m.id, m.watchlist_id, m.ticker, m.stock_name, m.reason, m.comment,
            m.signal_date, m.signal_price, m.added_at,
            w.name           AS watchlist_name,
            sd.close         AS current_price,
            sd.date          AS price_date,
            sd.ma6, sd.ma10, sd.ma30, sd.ma50, sd.ma200, sd.rsi14,
            sd.high_30d, sd.low_30d, sd.high_52wk, sd.low_52wk, sd.vol_ma10,
            sd.pct_change    AS day_pct_change,
            sd.direction,
            CASE
                WHEN m.signal_price > 0 AND sd.close IS NOT NULL
                THEN ROUND((sd.close - m.signal_price) / m.signal_price * 100, 2)
                ELSE NULL
            END AS since_signal_pct
        FROM monitor_list m
        LEFT JOIN watchlists w ON w.id = m.watchlist_id
        LEFT JOIN lp ON m.ticker = lp.ticker
        LEFT JOIN stocks_daily sd ON sd.ticker = lp.ticker AND sd.date = lp.max_date
        WHERE 1=1 {wl_clause} {ticker_sql}
        ORDER BY m.ticker
    """
    with get_connection() as conn:
        return _ok(_rows(conn, sql, wl_params + ticker_params))


# ---------------------------------------------------------------------------
# Monitor list CRUD + upload/download
# ---------------------------------------------------------------------------

@app.get("/api/monitor/tickers")
def list_monitor_tickers():
    watchlist_id = request.args.get("watchlist", type=int)
    if watchlist_id:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT ticker FROM monitor_list WHERE watchlist_id=? ORDER BY ticker",
                (watchlist_id,)
            ).fetchall()
    else:
        with get_connection() as conn:
            rows = conn.execute("SELECT ticker FROM monitor_list ORDER BY ticker").fetchall()
    return jsonify({"tickers": [r["ticker"] for r in rows]})


@app.post("/api/monitor")
def add_monitor():
    body         = request.get_json(silent=True) or {}
    ticker       = (body.get("ticker")       or "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    watchlist_id = body.get("watchlist_id", 1)
    try:
        watchlist_id = int(watchlist_id)
    except (TypeError, ValueError):
        watchlist_id = 1
    stock_name   = (body.get("stock_name")   or "").strip() or None
    reason       = (body.get("reason")       or "").strip() or None
    comment      = (body.get("comment")      or "").strip() or None
    signal_date  = _normalise_date((body.get("signal_date") or "").strip() or None)
    signal_price = body.get("signal_price")
    try:
        signal_price = float(signal_price) if signal_price is not None else None
    except (TypeError, ValueError):
        signal_price = None
    with get_connection() as conn:
        try:
            conn.execute(
                """INSERT INTO monitor_list
                   (watchlist_id, ticker, stock_name, reason, comment, signal_date, signal_price)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (watchlist_id, ticker, stock_name, reason, comment, signal_date, signal_price)
            )
        except Exception:
            return jsonify({"error": f"{ticker} already in this watchlist"}), 409
    return jsonify({"added": ticker}), 201


@app.put("/api/monitor/<int:rec_id>")
def update_monitor(rec_id):
    body         = request.get_json(silent=True) or {}
    ticker       = (body.get("ticker")       or "").strip().upper()
    stock_name   = (body.get("stock_name")   or "").strip() or None
    reason       = (body.get("reason")       or "").strip() or None
    comment      = (body.get("comment")      or "").strip() or None
    signal_date  = _normalise_date((body.get("signal_date") or "").strip() or None)
    signal_price = body.get("signal_price")
    try:
        signal_price = float(signal_price) if signal_price is not None and str(signal_price).strip() != "" else None
    except (TypeError, ValueError):
        signal_price = None
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE monitor_list
               SET ticker=?, stock_name=?, reason=?, comment=?, signal_date=?, signal_price=?
               WHERE id=?""",
            (ticker, stock_name, reason, comment, signal_date, signal_price, rec_id)
        )
    if cur.rowcount == 0:
        return jsonify({"error": "record not found"}), 404
    return jsonify({"updated": rec_id})


@app.delete("/api/monitor/<int:rec_id>")
def delete_monitor(rec_id):
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM monitor_list WHERE id = ?", (rec_id,))
    if cur.rowcount == 0:
        return jsonify({"error": "record not found"}), 404
    return jsonify({"deleted": rec_id})


@app.post("/api/monitor/upload")
def upload_monitor():
    return _upload_table(
        request,
        table="monitor_list",
        columns=["ticker", "stock_name", "reason", "comment"],
        upsert=True,
    )


@app.get("/api/monitor/download")
def download_monitor():
    watchlist_id = request.args.get("watchlist", type=int)
    with get_connection() as conn:
        if watchlist_id:
            rows = conn.execute(
                """SELECT ticker, stock_name, reason, comment, signal_date, signal_price, added_at
                   FROM monitor_list WHERE watchlist_id=? ORDER BY ticker""",
                (watchlist_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT ticker, stock_name, reason, comment, signal_date, signal_price, added_at
                   FROM monitor_list ORDER BY ticker"""
            ).fetchall()
    return _csv_response(rows, "monitor_list.csv")


# ---------------------------------------------------------------------------
# GET /api/insider   — insider signals stored by the pipeline
# ---------------------------------------------------------------------------

@app.get("/api/insider")
def get_insider():
    ticker_sql, ticker_params = _ticker_filter("s")
    from_date = (request.args.get("from") or "").strip()
    to_date   = (request.args.get("to")   or "").strip()
    date_sql, date_params = "", ()
    if from_date:
        date_sql  += " AND s.transaction_date >= ?"
        date_params += (from_date,)
    if to_date:
        date_sql  += " AND s.transaction_date <= ?"
        date_params += (to_date,)
    sql = f"""
        SELECT
            s.id, s.filed_date, s.transaction_date,
            s.ticker, s.company_name, s.insider_name, s.role,
            s.transaction_type, s.shares, s.price, s.total_value,
            s.cluster_buy, s.flag_10b51, s.filing_url, s.source,
            s.scan_run_at
        FROM insider_signals s
        WHERE 1=1 {ticker_sql} {date_sql}
        ORDER BY s.transaction_date DESC, s.total_value DESC
    """
    with get_connection() as conn:
        return _ok(_rows(conn, sql, ticker_params + date_params))


@app.delete("/api/insider/<int:rec_id>")
def delete_insider(rec_id):
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM insider_signals WHERE id = ?", (rec_id,))
    if cur.rowcount == 0:
        return jsonify({"error": "record not found"}), 404
    return jsonify({"deleted": rec_id})


# ---------------------------------------------------------------------------
# POST /api/insider/scan   GET /api/insider/scan/status
# ---------------------------------------------------------------------------

@app.post("/api/insider/scan")
def start_insider_scan():
    with _insider_scan_lock:
        if _insider_scan_state["running"]:
            return jsonify({"status": "already_running"}), 409

        body      = request.get_json(silent=True) or {}
        tickers   = (body.get("tickers") or "").strip()
        from_date = (body.get("from_date") or "").strip()
        to_date   = (body.get("to_date")   or datetime.now().strftime("%Y-%m-%d")).strip()

        if not tickers:
            return jsonify({"error": "tickers is required (e.g. NOK or NOK,ERIC)"}), 400
        if not from_date:
            return jsonify({"error": "from_date is required (YYYY-MM-DD)"}), 400

        _insider_scan_state.update({"running": True, "log": [], "error": None})

    cmd = [sys.executable, _INSIDER_PIPELINE,
           "--ticker", tickers, "--from", from_date, "--to", to_date]

    def _run():
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=os.path.dirname(_INSIDER_PIPELINE),
            )
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    _insider_scan_state["log"].append(line)
            proc.wait()
            if proc.returncode != 0:
                _insider_scan_state["error"] = f"Pipeline exited with code {proc.returncode}"
        except Exception as exc:
            _insider_scan_state["error"] = str(exc)
        finally:
            _insider_scan_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@app.get("/api/insider/scan/status")
def insider_scan_status():
    return jsonify({
        "running": _insider_scan_state["running"],
        "log":     list(_insider_scan_state["log"]),
        "error":   _insider_scan_state["error"],
    })


# ---------------------------------------------------------------------------
# GET /api/congress   — Congress (House + Senate) trade disclosures
# POST /api/congress/fetch   GET /api/congress/fetch/status
# ---------------------------------------------------------------------------

@app.get("/api/congress/tickers")
def get_congress_tickers():
    """Lightweight distinct-ticker list — powers the "this stock has Congress
    trading activity" badge on the Signals/Pattern Scanner/Monitor tabs
    without pulling full row data (mirrors /api/monitor/tickers)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM congress_trades WHERE ticker IS NOT NULL ORDER BY ticker"
        ).fetchall()
    return jsonify({"tickers": [r["ticker"] for r in rows]})


@app.get("/api/congress")
def get_congress():
    ticker_sql, ticker_params = _ticker_filter("c")
    politician = (request.args.get("politician") or "").strip()
    chamber = (request.args.get("chamber") or "").strip()
    party   = (request.args.get("party")   or "").strip()
    ttype   = (request.args.get("type")    or "").strip()
    from_date = (request.args.get("from") or "").strip()
    to_date   = (request.args.get("to")   or "").strip()

    extra_sql, extra_params = "", ()
    if politician:
        # SQLite LIKE is case-insensitive for ASCII by default — "donald"
        # matches "Donald J Trump" same as "Donald" or "DONALD" would.
        extra_sql += " AND c.politician_name LIKE ?"
        extra_params += (f"%{politician}%",)
    if chamber:
        extra_sql += " AND c.chamber = ?"
        extra_params += (chamber,)
    if party:
        extra_sql += " AND c.party = ?"
        extra_params += (party,)
    if ttype:
        extra_sql += " AND c.type = ?"
        extra_params += (ttype,)
    if from_date:
        extra_sql += " AND c.tx_date >= ?"
        extra_params += (from_date,)
    if to_date:
        extra_sql += " AND c.tx_date <= ?"
        extra_params += (to_date,)

    sql = f"""
        SELECT
            c.id, c.tx_date, c.filed_date, c.ticker, c.company,
            c.politician_name, c.chamber, c.party, c.state_or_district,
            c.role, c.type, c.amount_range, c.source, c.filing_url,
            c.cluster_buy, c.fetched_at
        FROM congress_trades c
        WHERE 1=1 {ticker_sql} {extra_sql}
        ORDER BY c.tx_date DESC, c.filed_date DESC
    """
    with get_connection() as conn:
        return _ok(_rows(conn, sql, ticker_params + extra_params))


_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


@app.get("/api/congress/summary")
def get_congress_summary():
    """Top-traded tickers (by BUY count and by SELL count) for a single
    month or an inclusive range of months — ?month=YYYY-MM or
    ?month_from=YYYY-MM&month_to=YYYY-MM (month_to defaults to month_from,
    i.e. a single month, when omitted). Optional ?chamber=&party= narrow
    both the top-lists and the totals to that subset."""
    month_from = (request.args.get("month_from") or request.args.get("month") or "").strip()
    month_to   = (request.args.get("month_to")   or month_from).strip()
    chamber    = (request.args.get("chamber") or "").strip()
    party      = (request.args.get("party")   or "").strip()
    limit      = request.args.get("limit", 10, type=int)
    limit      = max(1, min(limit, 50))

    if not _MONTH_RE.match(month_from) or not _MONTH_RE.match(month_to):
        return jsonify({"error": "month_from/month (and month_to, if given) must be YYYY-MM"}), 400
    if month_to < month_from:
        month_from, month_to = month_to, month_from

    start_date = f"{month_from}-01"
    # First day of the month AFTER month_to, as an exclusive upper bound —
    # avoids needing to know how many days are in month_to.
    y, m = (int(x) for x in month_to.split("-"))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    end_date_exclusive = f"{y:04d}-{m:02d}-01"

    extra_sql, extra_params = "", ()
    if chamber:
        extra_sql += " AND chamber = ?"
        extra_params += (chamber,)
    if party:
        extra_sql += " AND party = ?"
        extra_params += (party,)

    def _top(ttype):
        sql = f"""
            SELECT ticker, COUNT(*) AS trade_count,
                   COUNT(DISTINCT politician_name) AS politician_count
            FROM congress_trades
            WHERE type = ? AND ticker IS NOT NULL
              AND tx_date >= ? AND tx_date < ?
              {extra_sql}
            GROUP BY ticker
            ORDER BY trade_count DESC, politician_count DESC, ticker
            LIMIT ?
        """
        return _rows(conn, sql, (ttype, start_date, end_date_exclusive) + extra_params + (limit,))

    with get_connection() as conn:
        top_buys  = _top("BUY")
        top_sells = _top("SELL")
        totals = dict(conn.execute(f"""
            SELECT
                SUM(CASE WHEN type = 'BUY'  THEN 1 ELSE 0 END) AS total_buys,
                SUM(CASE WHEN type = 'SELL' THEN 1 ELSE 0 END) AS total_sells,
                COUNT(DISTINCT politician_name) AS distinct_politicians,
                COUNT(DISTINCT ticker) AS distinct_tickers
            FROM congress_trades
            WHERE tx_date >= ? AND tx_date < ? {extra_sql}
        """, (start_date, end_date_exclusive) + extra_params).fetchone())

    return jsonify({
        "month_from": month_from,
        "month_to":   month_to,
        "chamber":    chamber,
        "party":      party,
        "totals":     totals,
        "top_buys":   top_buys,
        "top_sells":  top_sells,
    })


@app.delete("/api/congress/<int:rec_id>")
def delete_congress(rec_id):
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM congress_trades WHERE id = ?", (rec_id,))
    if cur.rowcount == 0:
        return jsonify({"error": "record not found"}), 404
    return jsonify({"deleted": rec_id})


@app.post("/api/congress/fetch")
def start_congress_fetch():
    with _congress_fetch_lock:
        if _congress_fetch_state["running"]:
            return jsonify({"status": "already_running"}), 409
        _congress_fetch_state.update({"running": True, "log": [], "error": None, "summary": None})

    def _cb(msg: str):
        _congress_fetch_state["log"].append(msg)

    def _run():
        try:
            summary = fetch_congress_trades(progress_cb=_cb)
            _congress_fetch_state["summary"] = summary
        except Exception as exc:
            _congress_fetch_state["error"] = str(exc)
        finally:
            _congress_fetch_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@app.get("/api/congress/fetch/status")
def congress_fetch_status():
    return jsonify({
        "running": _congress_fetch_state["running"],
        "log":     list(_congress_fetch_state["log"]),
        "error":   _congress_fetch_state["error"],
        "summary": _congress_fetch_state["summary"],
    })


# ---------------------------------------------------------------------------
# GET /api/market/status   — Distribution Day / Follow-Through Day health
# ---------------------------------------------------------------------------

@app.get("/api/market/status")
def get_market_status():
    """
    Returns IBD-style market health: Distribution Day counts, Follow-Through
    Days, market status, and 60-day chart data for ^IXIC and ^GSPC.

    Cache: result is cached in memory for the current calendar day.
    Pass ?refresh=1 to force a new fetch from yfinance.
    """
    refresh = request.args.get("refresh", "0") in ("1", "true", "yes")
    today   = datetime.now().strftime("%Y-%m-%d")

    with _market_cache_lock:
        if not refresh and _market_cache["data"] and _market_cache["date"] == today:
            return jsonify(_market_cache["data"])

    try:
        from market_health import get_market_health
        data = get_market_health()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    with _market_cache_lock:
        _market_cache["data"] = data
        _market_cache["date"] = today

    return jsonify(data)


# ---------------------------------------------------------------------------
# GET /api/prices
# ---------------------------------------------------------------------------

@app.get("/api/prices")
def get_prices():
    ticker_sql, ticker_params = _ticker_filter("t")
    sql = f"""
        {_LATEST_PRICE_CTE}
        , all_tickers AS (
            SELECT ticker FROM holdings
            UNION
            SELECT ticker FROM monitor_list
        )
        SELECT
            t.ticker,
            sd.close         AS price,
            sd.date          AS price_date,
            sd.open, sd.high, sd.low,
            sd.volume, sd.vol_ma10,
            sd.pct_change    AS day_pct_change,
            sd.direction
        FROM all_tickers t
        LEFT JOIN lp ON t.ticker = lp.ticker
        LEFT JOIN stocks_daily sd ON sd.ticker = lp.ticker AND sd.date = lp.max_date
        WHERE 1=1 {ticker_sql}
        ORDER BY t.ticker
    """
    with get_connection() as conn:
        return _ok(_rows(conn, sql, ticker_params))


# ---------------------------------------------------------------------------
# Extraction ticker list  CRUD
# ---------------------------------------------------------------------------

@app.get("/api/extraction/tickers")
def list_extraction_tickers():
    with get_connection() as conn:
        rows = _rows(conn, "SELECT id, ticker, notes, added_at FROM extraction_tickers ORDER BY ticker")
    return jsonify({"count": len(rows), "tickers": rows})


@app.post("/api/extraction/tickers")
def add_extraction_ticker():
    body   = request.get_json(silent=True) or {}
    ticker = (body.get("ticker") or "").strip().upper()
    notes  = (body.get("notes") or "").strip() or None
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    with get_connection() as conn:
        try:
            conn.execute(
                "INSERT INTO extraction_tickers (ticker, notes) VALUES (?, ?)",
                (ticker, notes)
            )
        except Exception:
            return jsonify({"error": f"{ticker} already exists"}), 409
    return jsonify({"added": ticker}), 201


@app.delete("/api/extraction/tickers/<ticker>")
def delete_extraction_ticker(ticker):
    ticker = ticker.strip().upper()
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM extraction_tickers WHERE ticker = ?", (ticker,))
    if cur.rowcount == 0:
        return jsonify({"error": f"{ticker} not found"}), 404
    return jsonify({"deleted": ticker})


@app.delete("/api/extraction/tickers")
def clear_extraction_tickers():
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM extraction_tickers")
    return jsonify({"deleted": cur.rowcount})


# ---------------------------------------------------------------------------
# POST /api/extraction/upload  — bulk CSV / XLSX / JSON
# ---------------------------------------------------------------------------

@app.post("/api/extraction/upload")
def upload_extraction_tickers():
    tickers_to_add = []

    # --- JSON body path ---
    if request.is_json:
        body = request.get_json(silent=True) or {}
        raw  = body.get("tickers", [])
        tickers_to_add = [t.strip().upper() for t in raw if str(t).strip()]

    # --- File upload path ---
    elif "file" in request.files:
        f    = request.files["file"]
        name = (f.filename or "").lower()

        if name.endswith(".csv"):
            text    = f.read().decode("utf-8", errors="replace")
            reader  = csv.reader(io.StringIO(text))
            for row in reader:
                if row:
                    val = row[0].strip().upper()
                    if val and val != "TICKER":
                        tickers_to_add.append(val)

        elif name.endswith((".xlsx", ".xls")):
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(f.read()), read_only=True, data_only=True)
            ws = wb.active
            for row in ws.iter_rows(min_row=1, max_col=1, values_only=True):
                val = str(row[0] or "").strip().upper()
                if val and val != "TICKER":
                    tickers_to_add.append(val)
            wb.close()
        else:
            return jsonify({"error": "Only .csv and .xlsx files are supported"}), 400
    else:
        return jsonify({"error": "Send a file (CSV/XLSX) or JSON {\"tickers\":[...]}"}), 400

    if not tickers_to_add:
        return jsonify({"error": "No valid tickers found in upload"}), 400

    added = skipped = 0
    with get_connection() as conn:
        for t in tickers_to_add:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO extraction_tickers (ticker) VALUES (?)", (t,)
                )
                if conn.execute(
                    "SELECT changes()"
                ).fetchone()[0]:
                    added += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1

    return jsonify({"added": added, "skipped": skipped})


# ---------------------------------------------------------------------------
# GET /api/extraction/download  — download ticker list as CSV
# ---------------------------------------------------------------------------

@app.get("/api/extraction/download")
def download_extraction_tickers():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT ticker, notes, added_at FROM extraction_tickers ORDER BY ticker"
        ).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ticker", "notes", "added_at"])
    for r in rows:
        writer.writerow([r["ticker"], r["notes"] or "", r["added_at"]])

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=tickers.csv"},
    )


# ---------------------------------------------------------------------------
# POST /api/fetch   GET /api/fetch/status
# ---------------------------------------------------------------------------

@app.post("/api/fetch")
def start_fetch():
    with _fetch_lock:
        if _fetch_state["running"]:
            return jsonify({"status": "already_running"}), 409

        body          = request.get_json(silent=True) or {}
        period        = body.get("period", "2y")
        skip_universe = bool(body.get("skip_universe", False))
        if period not in VALID_PERIODS:
            return jsonify({"error": f"Invalid period. Use one of: {sorted(VALID_PERIODS)}"}), 400

        tickers = get_tickers_from_db()
        if not tickers:
            return jsonify({"error": "extraction_tickers table is empty. Add tickers first."}), 400

        _fetch_state.update({
            "running": True,
            "total":   len(tickers),
            "done":    0,
            "log":     [],
            "error":   None,
        })

    def _run():
        def _cb(msg: str):
            _fetch_state["log"].append(msg)
            if msg.startswith(("OK ", "SKIP ", "ERROR ")):
                _fetch_state["done"] += 1

        try:
            fetch_all(tickers, period=period, progress_cb=_cb, skip_universe=skip_universe)
        except Exception as exc:
            _fetch_state["error"] = str(exc)
        finally:
            _fetch_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started", "total": len(tickers)})


@app.get("/api/fetch/status")
def fetch_status():
    return jsonify({
        "running": _fetch_state["running"],
        "total":   _fetch_state["total"],
        "done":    _fetch_state["done"],
        "log":     list(_fetch_state["log"]),
        "error":   _fetch_state["error"],
    })


# ---------------------------------------------------------------------------
# Pattern scanner  POST /api/patterns/scan   GET /api/patterns/scan/status
#                  GET  /api/patterns/results  GET /api/patterns/dates
# ---------------------------------------------------------------------------

@app.post("/api/patterns/scan")
def start_pattern_scan():
    with _scan_lock:
        if _scan_state["running"]:
            return jsonify({"status": "already_running"}), 409
        body      = request.get_json(silent=True) or {}
        from_date = (body.get("from_date") or "").strip() or None
        to_date   = (body.get("to_date")   or "").strip() or None
        scan_date = (body.get("date")       or "").strip() or None
        is_range  = bool(from_date and to_date)
        try:
            squeeze_threshold = float(body["squeeze_threshold"]) if body.get("squeeze_threshold") not in (None, "") else None
        except (TypeError, ValueError):
            squeeze_threshold = None
        try:
            price_threshold = float(body["price_threshold"]) if body.get("price_threshold") not in (None, "") else None
        except (TypeError, ValueError):
            price_threshold = None
        try:
            max_squeeze_age = float(body["max_squeeze_age"]) if body.get("max_squeeze_age") not in (None, "") else None
        except (TypeError, ValueError):
            max_squeeze_age = None
        try:
            vol_surge_mult = float(body["vol_surge_mult"]) if body.get("vol_surge_mult") not in (None, "") else None
        except (TypeError, ValueError):
            vol_surge_mult = None

        if is_range:
            _scan_state.update({
                "running": True, "total": 0, "done": 0, "ticker": "",
                "error": None, "scan_date": to_date,
                "mode": "range", "from_date": from_date, "to_date": to_date,
            })
        else:
            scan_date = scan_date or datetime.now().strftime("%Y-%m-%d")
            _scan_state.update({
                "running": True, "total": 0, "done": 0, "ticker": "",
                "error": None, "scan_date": scan_date,
                "mode": "single", "from_date": None, "to_date": None,
            })

    def _run():
        def _cb(done, total, ticker):
            _scan_state["done"]   = done
            _scan_state["total"]  = total
            _scan_state["ticker"] = ticker
        try:
            if is_range:
                scan_date_range(from_date, to_date, progress_cb=_cb,
                                 squeeze_threshold=squeeze_threshold, price_threshold=price_threshold,
                                 max_squeeze_age=max_squeeze_age, vol_surge_mult=vol_surge_mult)
            else:
                scan_all_patterns(scan_date, progress_cb=_cb,
                                   squeeze_threshold=squeeze_threshold, price_threshold=price_threshold,
                                   max_squeeze_age=max_squeeze_age, vol_surge_mult=vol_surge_mult)
        except Exception as exc:
            _scan_state["error"] = str(exc)
        finally:
            _scan_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    if is_range:
        return jsonify({"status": "started", "from_date": from_date, "to_date": to_date, "mode": "range"})
    return jsonify({"status": "started", "scan_date": scan_date, "mode": "single"})


@app.get("/api/patterns/scan/status")
def pattern_scan_status():
    return jsonify({k: _scan_state[k] for k in
                    ("running", "total", "done", "ticker", "error", "scan_date",
                     "mode", "from_date", "to_date")})


@app.get("/api/patterns/results")
def get_pattern_results_api():
    from_date = (request.args.get("from") or "").strip()
    to_date   = (request.args.get("to")   or "").strip()
    if from_date and to_date:
        results = get_scan_results_range(from_date, to_date)
        return jsonify({"count": len(results), "data": results,
                        "from_date": from_date, "to_date": to_date})
    scan_date = (request.args.get("date") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    results   = get_scan_results(scan_date)
    return jsonify({"count": len(results), "data": results, "scan_date": scan_date})


@app.get("/api/patterns/dates")
def get_pattern_dates_api():
    return jsonify({"dates": get_available_scan_dates()})


# ---------------------------------------------------------------------------
# Backtest  POST /api/backtest/single   POST /api/backtest/batch
# ---------------------------------------------------------------------------

def _parse_rules(body):
    """Extract strategy percentage rules from request body (values as %)."""
    return {
        "t1":    float(body.get("t1",    10.0)) / 100.0,
        "sl":    float(body.get("sl",    11.0)) / 100.0,
        "t2":    float(body.get("t2",    20.0)) / 100.0,
        "rev":   float(body.get("rev",    0.0)) / 100.0,
        "prot":  float(body.get("prot",  10.0)) / 100.0,
        "trail": float(body.get("trail", 10.0)) / 100.0,
        "q1":    max(1, int(body.get("q1", 2))),
        "q2":    max(1, int(body.get("q2", 2))),
        "q3":    max(1, int(body.get("q3", 2))),
    }


@app.post("/api/backtest/single")
def backtest_single():
    body   = request.get_json(silent=True) or {}
    ticker = (body.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400

    try:
        p0         = float(body["p0"])
        start_date = (body.get("start_date") or "").strip()
        end_date   = (body.get("end_date")   or "").strip() or None
        rules      = _parse_rules(body)
    except (KeyError, ValueError, TypeError) as exc:
        return jsonify({"error": f"Invalid parameters: {exc}"}), 400

    if not start_date:
        return jsonify({"error": "start_date is required"}), 400

    with get_connection() as conn:
        if end_date:
            rows = conn.execute(
                "SELECT date, open, high, low, high_30d FROM stocks_daily"
                " WHERE ticker=? AND date>=? AND date<=? ORDER BY date",
                (ticker, start_date, end_date),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT date, open, high, low, high_30d FROM stocks_daily"
                " WHERE ticker=? AND date>=? ORDER BY date",
                (ticker, start_date),
            ).fetchall()

    if not rows:
        return jsonify({"error": f"No price data for {ticker} from {start_date}"}), 404

    txs = run_trading_simulation([dict(r) for r in rows], p0, start_date, rules)
    if not txs:
        return jsonify({"error": "Simulation produced no transactions"}), 404

    initial_cost, total_pnl, roi = calculate_metrics(txs)
    return jsonify({
        "ticker":       ticker,
        "initial_cost": round(initial_cost, 2),
        "total_pnl":    round(total_pnl,    2),
        "roi":          round(roi,           2),
        "status":       txs[-1]["action"],
        "transactions": txs,
    })


@app.post("/api/backtest/selection")
def backtest_selection():
    body  = request.get_json(silent=True) or {}
    items = body.get("items") or []
    if not items:
        return jsonify({"error": "items list is required"}), 400
    try:
        rules = _parse_rules(body)
    except (ValueError, TypeError) as exc:
        return jsonify({"error": f"Invalid parameters: {exc}"}), 400

    results = []
    for item in items:
        ticker     = (item.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        try:
            p0         = float(item["p0"])
            start_date = str(item.get("start_date") or "").strip()
        except (KeyError, ValueError, TypeError):
            results.append({"ticker": ticker, "error": "Invalid p0 or start_date"})
            continue

        if p0 <= 0:
            results.append({"ticker": ticker, "p0": p0, "start_date": start_date,
                             "error": "p0 must be > 0"})
            continue
        if not start_date:
            results.append({"ticker": ticker, "p0": p0, "start_date": start_date,
                             "error": "start_date is required"})
            continue

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT date, open, high, low, high_30d FROM stocks_daily"
                " WHERE ticker=? AND date>=? ORDER BY date",
                (ticker, start_date),
            ).fetchall()

        if not rows:
            results.append({"ticker": ticker, "p0": p0, "start_date": start_date,
                             "error": "No price data found",
                             "initial_cost": None, "total_pnl": None,
                             "roi": None, "status": None, "transactions": []})
            continue

        txs = run_trading_simulation([dict(r) for r in rows], p0, start_date, rules)
        if not txs:
            results.append({"ticker": ticker, "p0": p0, "start_date": start_date,
                             "error": "Simulation produced no transactions",
                             "initial_cost": None, "total_pnl": None,
                             "roi": None, "status": None, "transactions": []})
            continue

        initial_cost, total_pnl, roi = calculate_metrics(txs)
        results.append({
            "ticker":       ticker,
            "p0":           p0,
            "start_date":   start_date,
            "initial_cost": round(initial_cost, 2),
            "total_pnl":    round(total_pnl,    2),
            "roi":          round(roi,           2),
            "status":       txs[-1]["action"],
            "transactions": txs,
        })

    return jsonify({"count": len(results), "data": results})


@app.post("/api/backtest/batch")
def backtest_batch():
    body = request.get_json(silent=True) or {}
    try:
        rules = _parse_rules(body)
    except (ValueError, TypeError) as exc:
        return jsonify({"error": f"Invalid parameters: {exc}"}), 400

    with get_connection() as conn:
        holdings = conn.execute(
            "SELECT ticker, avg_buy_price, buy_date FROM holdings ORDER BY ticker"
        ).fetchall()

    if not holdings:
        return jsonify({"error": "No holdings found — add holdings first"}), 404

    results = []
    for h in holdings:
        ticker     = h["ticker"]
        p0         = float(h["avg_buy_price"] or 0)
        start_date = h["buy_date"] or datetime.now().strftime("%Y-%m-%d")

        if p0 <= 0:
            results.append({"ticker": ticker, "p0": p0, "start_date": start_date,
                             "error": "avg_buy_price is 0 or missing"})
            continue

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT date, open, high, low, high_30d FROM stocks_daily"
                " WHERE ticker=? AND date>=? ORDER BY date",
                (ticker, start_date),
            ).fetchall()

        if not rows:
            results.append({"ticker": ticker, "p0": p0, "start_date": start_date,
                             "error": "No price data found",
                             "initial_cost": None, "total_pnl": None,
                             "roi": None, "status": None, "transactions": []})
            continue

        txs = run_trading_simulation([dict(r) for r in rows], p0, start_date, rules)
        if not txs:
            results.append({"ticker": ticker, "p0": p0, "start_date": start_date,
                             "error": "Simulation produced no transactions",
                             "initial_cost": None, "total_pnl": None,
                             "roi": None, "status": None, "transactions": []})
            continue

        initial_cost, total_pnl, roi = calculate_metrics(txs)
        results.append({
            "ticker":       ticker,
            "p0":           p0,
            "start_date":   start_date,
            "initial_cost": round(initial_cost, 2),
            "total_pnl":    round(total_pnl,    2),
            "roi":          round(roi,           2),
            "status":       txs[-1]["action"],
            "transactions": txs,
        })

    return jsonify({"count": len(results), "data": results})


# ---------------------------------------------------------------------------
# Backup / Restore
# ---------------------------------------------------------------------------

@app.get("/api/backup/download")
def backup_download():
    from db_setup import DB_PATH
    if not os.path.exists(DB_PATH):
        return jsonify({"error": "Database file not found"}), 404
    return send_file(DB_PATH, as_attachment=True, download_name="stock_dashboard.db",
                     mimetype="application/octet-stream")


@app.post("/api/backup/restore")
def backup_restore():
    from db_setup import DB_PATH
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    header = f.read(16)
    if not header.startswith(b"SQLite format 3"):
        return jsonify({"error": "Not a valid SQLite database file"}), 400
    f.seek(0)
    # Write to a temp file first, then atomically replace
    tmp_path = DB_PATH + ".restore_tmp"
    try:
        f.save(tmp_path)
        os.replace(tmp_path, DB_PATH)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "message": "Database restored. Please refresh the page."})


# ---------------------------------------------------------------------------
# Data Browser — latest snapshot + full history
# ---------------------------------------------------------------------------

@app.route("/api/data/summary")
def data_summary():
    """Per-ticker snapshot as of ?date= (defaults to today), combining the
    user's tracked Ticker List (stocks_daily) with the full S&P 500 +
    Russell 1000 universe (universe_prices — see universe.py). For each
    ticker, returns its most recent row on or before that date. Tracked
    tickers take precedence when a ticker is in both sets (`tracked` field
    distinguishes the two); both tables carry the same OHLCV/indicator/Trend
    Template columns, so this is a straight UNION ALL rather than a Python
    merge."""
    as_of = (request.args.get("date") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    with get_connection() as conn:
        rows = conn.execute("""
            WITH tracked AS (
                SELECT s.ticker, s.date, s.open, s.high, s.low, s.close, s.volume,
                       s.ma10, s.ma30, s.ma50, s.ma150, s.ma200, s.rsi14,
                       s.high_30d, s.low_30d, s.high_52wk, s.low_52wk, s.vol_ma10,
                       s.pct_change, s.direction,
                       s.rs_raw, COALESCE(rr.rs_rating, s.rs_rank) AS rs_rank,
                       rr.rs_score, rr.rs_line_value, rr.rs_line_trend, rr.rs_leader,
                       s.c1, s.c2, s.c3, s.c4, s.c5, s.c6, s.c7, s.c8,
                       s.trend_score,
                       1 AS tracked
                FROM stocks_daily s
                INNER JOIN (
                    SELECT ticker, MAX(date) AS max_date
                    FROM stocks_daily
                    WHERE date <= ?
                    GROUP BY ticker
                ) latest ON s.ticker = latest.ticker AND s.date = latest.max_date
                LEFT JOIN rs_ratings rr ON rr.ticker = s.ticker AND rr.date = s.date
            ),
            universe AS (
                SELECT u.ticker, u.date, u.open, u.high, u.low, u.close, u.volume,
                       u.ma10, u.ma30, u.ma50, u.ma150, u.ma200, u.rsi14,
                       u.high_30d, u.low_30d, u.high_52wk, u.low_52wk, u.vol_ma10,
                       u.pct_change, u.direction,
                       u.rs_raw, COALESCE(rr.rs_rating, u.rs_rank) AS rs_rank,
                       rr.rs_score, rr.rs_line_value, rr.rs_line_trend, rr.rs_leader,
                       u.c1, u.c2, u.c3, u.c4, u.c5, u.c6, u.c7, u.c8,
                       u.trend_score,
                       0 AS tracked
                FROM universe_prices u
                INNER JOIN (
                    SELECT ticker, MAX(date) AS max_date
                    FROM universe_prices
                    WHERE date <= ?
                      AND ticker IN (SELECT ticker FROM ticker_universe WHERE is_active = 1)
                    GROUP BY ticker
                ) latest ON u.ticker = latest.ticker AND u.date = latest.max_date
                LEFT JOIN rs_ratings rr ON rr.ticker = u.ticker AND rr.date = u.date
                WHERE u.ticker NOT IN (SELECT ticker FROM extraction_tickers)
            )
            SELECT * FROM tracked
            UNION ALL
            SELECT * FROM universe
            ORDER BY ticker
        """, (as_of, as_of)).fetchall()

        spark_rows = conn.execute(f"""
            WITH ranked AS (
                SELECT ticker, date, rs_line_value,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
                FROM rs_ratings
                WHERE date <= ?
            )
            SELECT ticker, date, rs_line_value FROM ranked
            WHERE rn <= {RS_LINE_TREND_LOOKBACK} ORDER BY ticker, date
        """, (as_of,)).fetchall()

    sparklines = {}
    for r in spark_rows:
        sparklines.setdefault(r["ticker"], []).append(r["rs_line_value"])

    result = []
    for r in rows:
        row = dict(r)
        row["rs_line_history"] = sparklines.get(row["ticker"], [])
        result.append(row)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Universe admin (S&P 500 + Russell 1000 universe refresh/meta)
# ---------------------------------------------------------------------------

@app.get("/api/screener/meta")
def screener_meta():
    """Universe admin info: active count, last refresh date, source breakdown."""
    with get_connection() as conn:
        return jsonify(universe_mod.get_universe_meta(conn))


@app.post("/api/universe/refresh")
def universe_refresh():
    """Manual universe re-scrape — the automatic weekly check already
    piggybacks on the Fetch button; this is a convenience trigger."""
    with get_connection() as conn:
        summary = universe_mod.refresh_universe(conn)
    return jsonify(summary)


@app.route("/api/debug/trend-template")
def debug_trend_template():
    """Show last 5 rows for a ticker with all Trend Template fields for verification."""
    ticker = request.args.get("ticker", "").strip().upper()
    if not ticker:
        return jsonify({"error": "?ticker= required"}), 400
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT date, close,
                   ma50, ma150, ma200,
                   high_52wk, low_52wk,
                   rs_raw, rs_rank,
                   c1, c2, c3, c4, c5, c6, c7, c8, trend_score
            FROM stocks_daily
            WHERE ticker = ?
            ORDER BY date DESC
            LIMIT 5
        """, (ticker,)).fetchall()
    if not rows:
        return jsonify({"error": f"No data for {ticker}"}), 404
    labels = ["C1:Close>MA150&200", "C2:MA150>MA200", "C3:MA200↑1mo",
              "C4:MA50>MA150&200", "C5:25%>52wkLow", "C6:Within25%52wkHi",
              "C7:RSRank≥70", "C8:Close>MA50"]
    result = []
    for r in rows:
        rd = dict(r)
        rd["criteria_detail"] = {
            labels[i]: rd.get(f"c{i+1}") for i in range(8)
        }
        result.append(rd)
    return jsonify({"ticker": ticker, "rows": result})


@app.route("/api/data/history")
def data_history():
    """History for one ticker. Falls back to universe_prices (S&P500 +
    Russell 1000 universe — see universe.py) when the ticker isn't in the
    user's tracked stocks_daily set, since the merged Data tab now also
    lists untracked universe tickers."""
    ticker = request.args.get("ticker", "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    limit = min(int(request.args.get("limit", 252)), 1000)
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT date, open, high, low, close, volume,
                   ma10, ma30, ma50, ma200, pct_change, direction
            FROM stocks_daily
            WHERE ticker = ?
            ORDER BY date DESC
            LIMIT ?
        """, (ticker, limit)).fetchall()
        if not rows:
            rows = conn.execute("""
                SELECT date, open, high, low, close, volume,
                       ma10, ma30, ma50, ma200, pct_change, direction
                FROM universe_prices
                WHERE ticker = ?
                ORDER BY date DESC
                LIMIT ?
            """, (ticker, limit)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/volume-profile")
def volume_profile():
    """Volume profile (POC + top-N high-volume support/resistance levels) for
    one ticker, cross-referenced against pivot-based S/R channels — see
    volume_profile.py. Falls back to universe_prices the same way
    /api/data/history does when the ticker isn't in the tracked set."""
    ticker = request.args.get("ticker", "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    lookback = min(max(int(request.args.get("lookback", VP_LOOKBACK_DAYS)), 10), 500)
    top_n    = min(max(int(request.args.get("top_n", VP_TOP_N)), 1), 20)

    # need enough rows to cover whichever lookback (volume or pivot) is longer,
    # plus slack on each side for the pivot window to find swing points near the edges
    fetch_rows = max(lookback, VP_PIVOT_LOOKBACK_DAYS) + VP_PIVOT_WINDOW * 2 + 20

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT date, open, high, low, close, volume
            FROM stocks_daily WHERE ticker = ? ORDER BY date DESC LIMIT ?
        """, (ticker, fetch_rows)).fetchall()
        if not rows:
            rows = conn.execute("""
                SELECT date, open, high, low, close, volume
                FROM universe_prices WHERE ticker = ? ORDER BY date DESC LIMIT ?
            """, (ticker, fetch_rows)).fetchall()
    if not rows:
        return jsonify({"error": f"No price data for {ticker}"}), 404

    price_rows = [dict(r) for r in reversed(rows)]  # ascending, as analyze() expects
    result = analyze_volume_profile(ticker, price_rows, config={"lookback_days": lookback, "top_n": top_n})
    return jsonify(result)


# ---------------------------------------------------------------------------
# Surge Strategy (Volume Surge, Scenario A) — signal board + marked positions.
# Engine + rules live in surge_strategy.py; these routes are the workflow:
#   scan -> (watching / armed / triggered) -> user marks "bought" -> position
#   -> user marks partial / sold. The 30-min live monitor is a later phase.
# ---------------------------------------------------------------------------

_surge_scan_lock = threading.Lock()
_surge_last_scan = {"at": None, "summary": None}


def _surge_num(val, name, required=True):
    """Parse a positive float from a JSON body; returns (value, error)."""
    if val in (None, ""):
        return (None, f"{name} is required") if required else (None, None)
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None, f"{name} must be a number"
    if v <= 0:
        return None, f"{name} must be greater than 0"
    return v, None


def _surge_date(val):
    """Normalised YYYY-MM-DD (today when blank), or None if unparseable."""
    if not val:
        return datetime.now().strftime("%Y-%m-%d")
    return _normalise_date(val)


def _surge_trading_dates(conn, n=80):
    rows = conn.execute("SELECT DISTINCT date FROM stocks_daily ORDER BY date DESC LIMIT ?", (n,)).fetchall()
    return sorted(r[0] for r in rows)


def _surge_latest_daily(conn, tickers):
    """{ticker: {date, close, ma21}} from stored daily bars (end-of-day; the
    live 30-min refresh is a later phase). MA21 = simple mean of the last 21 closes."""
    out = {}
    for t in tickers:
        rows = conn.execute(
            "SELECT date, close FROM stocks_daily WHERE ticker=? AND close IS NOT NULL "
            "ORDER BY date DESC LIMIT 21", (t,)).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT date, close FROM universe_prices WHERE ticker=? AND close IS NOT NULL "
                "ORDER BY date DESC LIMIT 21", (t,)).fetchall()
        if not rows:
            continue
        ma21 = sum(r["close"] for r in rows) / 21 if len(rows) == 21 else None
        out[t] = {"date": rows[0]["date"], "close": rows[0]["close"], "ma21": ma21}
    return out


def _surge_position_view(p, daily):
    """Position row + last price / MA21 / P&L. Prefers the live monitor's values
    (Yahoo, ~15 min delayed) when they are at least as recent as the stored daily
    bar; otherwise falls back to the stored end-of-day close. P&L is blended across
    the partial and remaining halves (same convention as the backtest); an open
    position's remaining half is marked at the last price."""
    d = dict(p)
    bp = d["buy_price"]
    last = daily.get(d["ticker"])
    use_live = (d.get("last_price") is not None and d.get("price_asof")
                and (not last or d["price_asof"] >= last["date"]))
    if use_live:
        price, price_date, ma21 = d["last_price"], d["price_asof"], d.get("last_ma21")
    else:
        price = last["close"] if last else None
        price_date = last["date"] if last else None
        ma21 = last["ma21"] if last else None
    d["last_close"] = price
    d["last_close_date"] = price_date
    d["price_is_live"] = bool(use_live and d.get("price_is_live"))
    d["ma21"] = round(ma21, 4) if ma21 is not None else None
    d["dist_ma21_pct"] = round((price - ma21) / ma21 * 100, 2) if price is not None and ma21 else None
    d["below_ma21"] = bool(price is not None and ma21 and price < ma21)
    final = d["status"] == "closed"
    rest = d["exit_price"] if final else price
    pnl = None
    if bp and rest:
        if d["partial_price"]:
            pnl = 0.5 * (d["partial_price"] / bp - 1) + 0.5 * (rest / bp - 1)
        else:
            pnl = rest / bp - 1
    d["pnl_pct"] = round(pnl * 100, 2) if pnl is not None else None
    d["pnl_final"] = final
    return d


def _surge_signals_with_age(conn):
    """Active signals + trading-day ages (days since surge / since trigger, days
    until a triggered signal goes stale)."""
    cfg = surge_strategy.CONFIG
    tdates = _surge_trading_dates(conn)
    signals = [dict(r) for r in conn.execute(
        "SELECT * FROM surge_signals WHERE status IN ('watching','armed','triggered') "
        "ORDER BY surge_date DESC, ticker")]
    for s in signals:
        s["days_since_surge"] = sum(1 for d in tdates if d > s["surge_date"])
        if s["trigger_date"]:
            since = sum(1 for d in tdates if d > s["trigger_date"])
            s["days_since_trigger"] = since
            stale = cfg["STALE_AFTER_TRIGGER_DAYS"]
            s["expires_in"] = (stale - since) if stale is not None else None
    return signals


def _surge_alerts(signals, positions):
    """Things that need the user's attention now. `key` is stable per event so the
    browser can notify once: sell / warn / target on positions, buy on signals."""
    out = []
    for p in positions:
        if p["status"] == "closed":
            continue
        if p["alert_state"] == "sell":
            out.append({"key": f"pos{p['id']}:sell:{p['ma21_break_date']}", "level": "sell",
                        "ticker": p["ticker"], "message": p["alert_note"] or "MA21 cut-loss confirmed — sell"})
        elif p["alert_state"] == "below_ma21":
            out.append({"key": f"pos{p['id']}:warn:{p['ma21_break_date'] or p['price_asof']}", "level": "warn",
                        "ticker": p["ticker"], "message": p["alert_note"] or "below MA21"})
        if p["status"] == "holding" and p.get("target_hit_date"):
            out.append({"key": f"pos{p['id']}:target", "level": "target", "ticker": p["ticker"],
                        "message": f"+15% target ${p['partial_target']:.2f} reached ({p['target_hit_date']}) — sell half"})
    for s in signals:
        if s["status"] == "triggered" and (s.get("expires_in") is None or s["expires_in"] >= 0):
            out.append({"key": f"sig{s['id']}:buy", "level": "buy", "ticker": s["ticker"],
                        "message": f"Buy Signal — limit ${s['buy_level']:.2f} reached ({s['trigger_date']})"})
    return out


def _surge_monitor_status():
    clock = surge_strategy.market_clock()
    nxt = surge_strategy.next_run_estimate()
    m = surge_strategy.MONITOR
    return {"enabled": m["enabled"], "running": m["running"], "last_run": m["last_run"],
            "last_reason": m["last_reason"], "last_error": m["last_error"],
            "last_summary": m["last_summary"], "market_open": clock["is_open"],
            "now_et": clock["now"].strftime("%Y-%m-%d %H:%M"),
            "next_run": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None}


def _surge_positions_view(conn):
    pos_rows = conn.execute(
        "SELECT * FROM surge_positions ORDER BY (status='closed'), buy_date DESC, id DESC").fetchall()
    daily = _surge_latest_daily(conn, {p["ticker"] for p in pos_rows})
    return [_surge_position_view(p, daily) for p in pos_rows]


@app.route("/api/surge/board")
def surge_board():
    """Everything the Surge Strategy tab renders in one call."""
    with get_connection() as conn:
        signals = _surge_signals_with_age(conn)
        positions = _surge_positions_view(conn)
        counts = {st: 0 for st in ("watching", "armed", "triggered")}
        for s in signals:
            counts[s["status"]] += 1
        latest = conn.execute("SELECT MAX(date) FROM stocks_daily").fetchone()[0]
    return jsonify({
        "signals": signals,
        "positions": [p for p in positions if p["status"] != "closed"],
        "closed": [p for p in positions if p["status"] == "closed"],
        "counts": counts,
        "config": surge_strategy.CONFIG,
        "data_through": latest,
        "last_scan": _surge_last_scan,
        "alerts": _surge_alerts(signals, positions),
        "monitor": _surge_monitor_status(),
    })


@app.route("/api/surge/alerts")
def surge_alerts():
    """Lightweight poll target (the page hits it every minute from any tab)."""
    with get_connection() as conn:
        signals = _surge_signals_with_age(conn)
        positions = _surge_positions_view(conn)
    return jsonify({"alerts": _surge_alerts(signals, positions), "monitor": _surge_monitor_status()})


@app.route("/api/surge/monitor/status")
def surge_monitor_status():
    return jsonify(_surge_monitor_status())


@app.route("/api/surge/monitor/refresh", methods=["POST"])
def surge_monitor_refresh():
    """Run one live pass now (positions + armed signals). Needs internet (Yahoo)."""
    try:
        summary = surge_strategy.run_refresh("manual")
    except Exception as exc:
        return jsonify({"error": f"live refresh failed: {exc}"}), 502
    if summary is None:
        return jsonify({"error": "A live refresh is already running"}), 409
    return jsonify(summary)


@app.route("/api/surge/scan", methods=["POST"])
def surge_scan():
    """Scan stored daily data for new surge signals + refresh existing ones.
    Synchronous (a few seconds); it only reads what the last Fetch stored."""
    if not _surge_scan_lock.acquire(blocking=False):
        return jsonify({"error": "A surge scan is already running"}), 409
    try:
        summary = surge_strategy.scan_signals()
    finally:
        _surge_scan_lock.release()
    if "error" in summary:
        return jsonify(summary), 400
    _surge_last_scan["at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _surge_last_scan["summary"] = {
        "as_of": summary["as_of"], "tickers_scanned": summary["tickers_scanned"],
        "new": len(summary["new"]), "changed": len(summary["changed"]),
    }
    return jsonify({**summary, "at": _surge_last_scan["at"]})


@app.route("/api/surge/signals/<int:sid>/bought", methods=["POST"])
def surge_mark_bought(sid):
    """User confirms they actually bought this signal -> creates a position."""
    b = request.get_json(silent=True) or {}
    price, err = _surge_num(b.get("buy_price"), "buy_price")
    if err:
        return jsonify({"error": err}), 400
    shares, err = _surge_num(b.get("shares"), "shares", required=False)
    if err:
        return jsonify({"error": err}), 400
    buy_date = _surge_date(b.get("buy_date"))
    if not buy_date:
        return jsonify({"error": "buy_date not recognised"}), 400
    with get_connection() as conn:
        sig = conn.execute("SELECT * FROM surge_signals WHERE id=?", (sid,)).fetchone()
        if not sig:
            return jsonify({"error": "signal not found"}), 404
        if conn.execute("SELECT 1 FROM surge_positions WHERE signal_id=?", (sid,)).fetchone():
            return jsonify({"error": "already marked as bought"}), 409
        target = round(sig["surge_close"] * (1 + surge_strategy.CONFIG["PARTIAL_TARGET_PCT"]), 4)
        cur = conn.execute(
            """INSERT INTO surge_positions
               (signal_id, ticker, surge_date, surge_close, buy_date, buy_price, shares,
                partial_target, status, note)
               VALUES (?,?,?,?,?,?,?,?,'holding',?)""",
            (sid, sig["ticker"], sig["surge_date"], sig["surge_close"], buy_date, price,
             shares, target, (b.get("note") or None)))
        conn.execute("UPDATE surge_signals SET status='bought', updated_at=CURRENT_TIMESTAMP WHERE id=?", (sid,))
        conn.commit()
    return jsonify({"ok": True, "position_id": cur.lastrowid, "partial_target": target}), 201


@app.route("/api/surge/signals/<int:sid>/dismiss", methods=["POST"])
def surge_dismiss(sid):
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE surge_signals SET status='dismissed', updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status IN ('watching','armed','triggered','expired')", (sid,))
        conn.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "signal not found or already bought"}), 404
    return jsonify({"ok": True})


@app.route("/api/surge/positions/<int:pid>/partial", methods=["POST"])
def surge_mark_partial(pid):
    """Half of the position sold at the +15% target."""
    b = request.get_json(silent=True) or {}
    price, err = _surge_num(b.get("price"), "price")
    if err:
        return jsonify({"error": err}), 400
    date = _surge_date(b.get("date"))
    if not date:
        return jsonify({"error": "date not recognised"}), 400
    with get_connection() as conn:
        p = conn.execute("SELECT * FROM surge_positions WHERE id=?", (pid,)).fetchone()
        if not p:
            return jsonify({"error": "position not found"}), 404
        if p["status"] != "holding":
            return jsonify({"error": f"position is {p['status']}, expected holding"}), 409
        conn.execute(
            "UPDATE surge_positions SET status='partial_sold', partial_date=?, partial_price=?, "
            "target_hit_date=NULL WHERE id=?",
            (date, price, pid))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/surge/positions/<int:pid>/sell", methods=["POST"])
def surge_mark_sold(pid):
    """Close the position (whatever is left) — cut-loss, manual, or otherwise."""
    b = request.get_json(silent=True) or {}
    price, err = _surge_num(b.get("price"), "price")
    if err:
        return jsonify({"error": err}), 400
    date = _surge_date(b.get("date"))
    if not date:
        return jsonify({"error": "date not recognised"}), 400
    reason = (b.get("reason") or "manual").strip()[:40]
    with get_connection() as conn:
        p = conn.execute("SELECT * FROM surge_positions WHERE id=?", (pid,)).fetchone()
        if not p:
            return jsonify({"error": "position not found"}), 404
        if p["status"] == "closed":
            return jsonify({"error": "position already closed"}), 409
        conn.execute(
            "UPDATE surge_positions SET status='closed', exit_date=?, exit_price=?, exit_reason=? WHERE id=?",
            (date, price, reason, pid))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/surge/positions/<int:pid>", methods=["DELETE"])
def surge_delete_position(pid):
    """Undo a mistaken 'I bought' — the signal returns to the board (the next
    scan re-evaluates it, so it may show as expired if it has gone stale)."""
    with get_connection() as conn:
        p = conn.execute("SELECT signal_id FROM surge_positions WHERE id=?", (pid,)).fetchone()
        if not p:
            return jsonify({"error": "position not found"}), 404
        conn.execute("DELETE FROM surge_positions WHERE id=?", (pid,))
        if p["signal_id"]:
            conn.execute("UPDATE surge_signals SET status='triggered', updated_at=CURRENT_TIMESTAMP "
                         "WHERE id=? AND status='bought'", (p["signal_id"],))
        conn.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stock Dashboard API")
    parser.add_argument("--host",  default="127.0.0.1")
    parser.add_argument("--port",  type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--user",  default=os.environ.get("DASH_USER", ""),
                        help="Enable Basic Auth with this username")
    parser.add_argument("--pass",  dest="password",
                        default=os.environ.get("DASH_PASS", ""),
                        help="Basic Auth password")
    args = parser.parse_args()

    if args.user:
        _AUTH_USER = args.user
        _AUTH_PASS = args.password
        print(f"  Basic Auth ENABLED  (user: {_AUTH_USER})")
    else:
        print("  Basic Auth disabled — use --user / --pass to protect the dashboard")

    setup_database()
    print(f"\nStock Dashboard API running at http://{args.host}:{args.port}")
    print("Endpoints:")
    print("  GET  /api/holdings")
    print("  GET  /api/signals/ma200?days=30")
    print("  GET  /api/signals/ma100?days=30")
    print("  GET  /api/signals/2xlow?multiplier=2.0")
    print("  GET  /api/signals/2xlow/first?multiplier=2.0")
    print("  GET  /api/signals/ma1030?days=30")
    print("  GET  /api/sold  |  /api/monitor  |  /api/prices")
    print("  GET  /api/extraction/tickers  (list)")
    print("  POST /api/extraction/tickers  (add one)")
    print("  POST /api/extraction/upload   (bulk CSV/XLSX/JSON)")
    print("  GET  /api/extraction/download (download CSV)")
    print("  POST /api/fetch               (start fetch)")
    print("  GET  /api/fetch/status        (poll progress)")
    print("  GET  /api/congress            (Congress trade disclosures)")
    print("  POST /api/congress/fetch      (fetch latest snapshot)\n")
    # 30-min live monitor for Surge Strategy positions. With --debug the reloader runs this
    # file twice (parent + child); only the child that actually serves requests starts it.
    if not args.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        if surge_strategy.start_monitor_thread():
            print("  Surge Strategy live monitor started (every 30 min, US market hours)")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
