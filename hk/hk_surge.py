"""
Hong Kong Volume Surge strategy (Scenario A) — the HK counterpart of surge_strategy.py.

Same rules as the US engine (it reuses that module's pure detection / exit functions):
  surge day -> red-candle drop day -> limit-buy at the surge close -> +15% partial sell /
  MA21 cut-loss (close below MA21 then next open still below).
What is different for Hong Kong:
  * data + tables live in hk/hk_stocks.db (hk_daily, hk_securities, hk_surge_signals,
    hk_surge_positions) — nothing here reads or writes stock_dashboard.db;
  * signals are filtered by market cap (close x shares >= MIN_MCAP) and liquidity
    (10-day average turnover >= MIN_TURNOVER), both measured the day BEFORE the surge;
  * live monitor runs on Hong Kong hours (Asia/Hong_Kong, lunch break skipped);
  * prices are HKD, tickers look like 0700.HK.

Run standalone:  python hk/hk_surge.py [--as-of YYYY-MM-DD]      (scan stored HK data)
"""
import argparse
import os
import sqlite3
import sys
import threading
import time as _time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import surge_strategy as ss          # pure functions only: is_surge / evaluate_signal / evaluate_position ...
import hk_fetch

DB_PATH = hk_fetch.DB_PATH
HKT = ZoneInfo("Asia/Hong_Kong")

CONFIG = dict(ss.CONFIG)
CONFIG.update({
    "MIN_MCAP": 1_000_000_000,     # HKD, close x shares outstanding the day before the surge
    "MIN_TURNOVER": 5_000_000,     # HKD per day (10-day avg volume x close, day before the surge)
    "SLOTS": 20,                   # max open positions
    "BASE_STAKE": 10_000,          # HKD per slot at the start
    "MAX_POS_PCT": 0.10,           # one position <= 10% of account equity
    "COST_PER_SIDE_PCT": 0.1605,   # stamp duty .10 + fees .0105 + assumed broker .05, % of value, each side
})

ACTIVE_STATUSES = ss.ACTIVE_STATUSES

SCHEMA = """
CREATE TABLE IF NOT EXISTS hk_surge_signals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    name              TEXT,
    surge_date        TEXT NOT NULL,
    surge_close       REAL NOT NULL,
    surge_vol_ratio   REAL,
    mcap              REAL,
    turnover          REAL,
    status            TEXT NOT NULL,
    drop_date         TEXT,
    buy_level         REAL,
    window_days_left  INTEGER,
    trigger_date      TEXT,
    note              TEXT,
    last_price        REAL,
    price_asof        TEXT,
    price_is_live     INTEGER DEFAULT 0,
    last_checked      TEXT,
    first_seen        TEXT,
    updated_at        TEXT,
    UNIQUE(ticker, surge_date)
);
CREATE INDEX IF NOT EXISTS idx_hk_surge_signals_status ON hk_surge_signals(status);
CREATE TABLE IF NOT EXISTS hk_surge_positions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id          INTEGER UNIQUE REFERENCES hk_surge_signals(id) ON DELETE CASCADE,
    ticker             TEXT NOT NULL,
    name               TEXT,
    surge_date         TEXT NOT NULL,
    surge_close        REAL NOT NULL,
    buy_date           TEXT NOT NULL,
    buy_price          REAL NOT NULL,
    shares             REAL,
    amount             REAL,               -- HKD paid (gross), optional; drives account/sizing figures
    partial_target     REAL,
    partial_date       TEXT,
    partial_price      REAL,
    status             TEXT NOT NULL DEFAULT 'holding',
    exit_date          TEXT,
    exit_price         REAL,
    exit_reason        TEXT,
    last_price         REAL,
    last_ma21          REAL,
    last_checked       TEXT,
    alert_state        TEXT DEFAULT 'ok',
    ma21_break_date    TEXT,
    alert_note         TEXT,
    target_hit_date    TEXT,
    price_asof         TEXT,
    price_is_live      INTEGER DEFAULT 0,
    note               TEXT,
    created_at         TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    _ensure_columns(conn)
    return conn


def _ensure_columns(conn):
    """Add columns introduced after the table first shipped (no-op when the table doesn't exist yet)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(hk_surge_signals)")}
    if cols:
        for col, typ in (("last_price", "REAL"), ("price_asof", "TEXT"),
                         ("price_is_live", "INTEGER DEFAULT 0"), ("last_checked", "TEXT")):
            if col not in cols:
                conn.execute(f"ALTER TABLE hk_surge_signals ADD COLUMN {col} {typ}")


def setup_db(conn=None):
    own = conn is None
    conn = conn or connect()
    conn.executescript(SCHEMA)
    conn.commit()
    if own:
        conn.close()


def _cfg(config=None):
    cfg = dict(CONFIG)
    if config:
        cfg.update(config)
    return cfg


# ---------------------------------------------------------------------------
# Data + scan
# ---------------------------------------------------------------------------

def load_series(conn, as_of=None, since=None):
    where, params = ["close IS NOT NULL"], []
    if as_of:
        where.append("date <= ?")
        params.append(as_of)
    if since:
        where.append("date >= ?")
        params.append(since)
    rows = conn.execute(
        "SELECT ticker, date, open, high, low, close, volume, vol_ma10, ma50, ma200 FROM hk_daily "
        f"WHERE {' AND '.join(where)} ORDER BY ticker, date", params).fetchall()
    by_ticker = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(dict(r))
    return by_ticker


def passes_filters(series, i, shares, cfg):
    """Market cap and liquidity measured on the day before the surge (i - 1)."""
    if i < 1 or not shares:
        return False, None, None
    prev = series[i - 1]
    if not prev["close"] or not prev["vol_ma10"]:
        return False, None, None
    mcap = prev["close"] * shares
    turnover = prev["vol_ma10"] * prev["close"]
    return (mcap >= cfg["MIN_MCAP"] and turnover >= cfg["MIN_TURNOVER"]), mcap, turnover


def scan_signals(as_of=None, config=None):
    """Find new HK surge days in the last LOOKBACK_DAYS trading days (that pass the market-cap
    and turnover filters) and refresh every watching/armed/triggered signal. Idempotent."""
    cfg = _cfg(config)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        latest = conn.execute("SELECT MAX(date) FROM hk_daily").fetchone()[0]
        if not latest:
            return {"error": "no HK price data - run the HK fetch first"}
        ref = min(as_of, latest) if as_of else latest
        since = (datetime.strptime(ref, "%Y-%m-%d") - timedelta(days=90)).strftime("%Y-%m-%d")
        by_ticker = load_series(conn, as_of=ref, since=since)
        forming = 0 if as_of else ss.drop_forming_bars(by_ticker, hk_clock())     # today's bar is partial until the close
        info = {r["ticker"]: (r["shares"], r["name"]) for r in conn.execute("SELECT ticker, shares, name FROM hk_securities")}

        active = {}
        for r in conn.execute("SELECT ticker, surge_date FROM hk_surge_signals WHERE status IN ('watching','armed','triggered')"):
            active.setdefault(r["ticker"], set()).add(r["surge_date"])

        now = datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S")
        summary = {"as_of": ref, "tickers_scanned": len(by_ticker), "new": [], "changed": [],
                   "dead_on_arrival": 0, "filtered_out": 0, "forming_bars_ignored": forming}
        for ticker, series in by_ticker.items():
            shares, name = info.get(ticker, (None, None))
            date_idx = {r["date"]: i for i, r in enumerate(series)}
            new_idx = set(ss.find_surge_indices(series, cfg, last_n=cfg["LOOKBACK_DAYS"]))
            idxs = set(new_idx) | {date_idx[d] for d in active.get(ticker, ()) if d in date_idx}
            for i in sorted(idxs):
                sdate = series[i]["date"]
                row = conn.execute("SELECT id, status, note FROM hk_surge_signals WHERE ticker=? AND surge_date=?",
                                   (ticker, sdate)).fetchone()
                ev = ss.evaluate_signal(series, i, cfg)
                if row is None:
                    ok, mcap, turnover = passes_filters(series, i, shares, cfg)
                    if not ok:
                        summary["filtered_out"] += 1
                        continue
                    if ev["status"] == "expired":
                        summary["dead_on_arrival"] += 1
                        continue
                    vr = (series[i]["volume"] / series[i]["vol_ma10"]) if series[i]["vol_ma10"] else None
                    conn.execute(
                        """INSERT INTO hk_surge_signals
                           (ticker, name, surge_date, surge_close, surge_vol_ratio, mcap, turnover, status, drop_date,
                            buy_level, window_days_left, trigger_date, note, first_seen, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (ticker, name, sdate, series[i]["close"], round(vr, 2) if vr else None, mcap, turnover,
                         ev["status"], ev["drop_date"], ev["buy_level"], ev["window_days_left"], ev["trigger_date"],
                         ev["note"], now, now))
                    summary["new"].append((ticker, sdate, ev["status"]))
                elif (row["status"] == "triggered" and ev["status"] in ("armed", "watching")
                      and row["note"] == ss.LIVE_TRIGGER_NOTE):
                    continue          # triggered live by the monitor before its daily bar was stored — keep it
                elif row["status"] in ACTIVE_STATUSES:
                    conn.execute(
                        """UPDATE hk_surge_signals SET status=?, drop_date=?, buy_level=?, window_days_left=?,
                               trigger_date=?, note=?, updated_at=? WHERE id=?""",
                        (ev["status"], ev["drop_date"], ev["buy_level"], ev["window_days_left"], ev["trigger_date"],
                         ev["note"], now, row["id"]))
                    if ev["status"] != row["status"]:
                        summary["changed"].append((ticker, sdate, row["status"], ev["status"]))
        conn.commit()
        return summary
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Live monitor (Hong Kong hours)
# ---------------------------------------------------------------------------
MONITOR_INTERVAL_MIN = 30
OPEN_CHECK = dtime(9, 45)          # first pass, once the open print exists
LUNCH = (dtime(12, 5), dtime(12, 55))
POST_CLOSE_RUN = dtime(16, 20)     # closing auction ends 16:10 — today's bar is final
POST_CLOSE_END = dtime(18, 0)

MONITOR = {"enabled": False, "running": False, "last_run": None, "last_run_dt": None, "last_reason": None,
           "last_error": None, "last_summary": None, "post_close_done": None}
_monitor_lock = threading.Lock()


def hk_clock(now=None):
    now = now or datetime.now(HKT)
    weekday = now.weekday() < 5
    t = now.time()
    return {"now": now, "today": now.strftime("%Y-%m-%d"), "weekday": weekday,
            "is_open": weekday and (dtime(9, 30) <= t < dtime(12, 0) or dtime(13, 0) <= t < dtime(16, 0)),
            "is_final": t >= POST_CLOSE_RUN}


def refresh_live(now=None, bars_by_ticker=None):
    """One live pass over open HK positions + armed signals (Yahoo daily bars, ~15 min delayed)."""
    clock = hk_clock(now)
    stamp = clock["now"].strftime("%Y-%m-%d %H:%M:%S")
    summary = {"checked_at": stamp, "positions": 0, "armed": 0, "watching": 0, "triggered": [], "drop_day": [],
               "sell": [], "warn": [], "errors": []}
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        positions = [dict(r) for r in conn.execute("SELECT * FROM hk_surge_positions WHERE status IN ('holding','partial_sold')")]
        armed = [dict(r) for r in conn.execute("SELECT * FROM hk_surge_signals WHERE status='armed'")]
        watching = [dict(r) for r in conn.execute("SELECT * FROM hk_surge_signals WHERE status='watching'")]
        triggered = [dict(r) for r in conn.execute("SELECT * FROM hk_surge_signals WHERE status='triggered'")]
    finally:
        conn.close()
    tickers = sorted({p["ticker"] for p in positions} | {s["ticker"] for s in armed}
                     | {s["ticker"] for s in watching} | {s["ticker"] for s in triggered})
    if not tickers:
        return summary
    if bars_by_ticker is None:
        bars_by_ticker = ss.fetch_live_bars(tickers)          # network, outside any DB transaction

    conn = connect()
    try:
        for p in positions:
            bars = bars_by_ticker.get(p["ticker"])
            if not bars:
                summary["errors"].append(f"{p['ticker']}: no price data")
                continue
            ev = ss.evaluate_position(bars, p["buy_date"], p["partial_target"], p["status"] == "partial_sold",
                                      clock["today"], clock["is_final"])
            conn.execute(
                """UPDATE hk_surge_positions SET last_price=?, last_ma21=?, last_checked=?, alert_state=?,
                       ma21_break_date=?, alert_note=?, target_hit_date=?, price_asof=?, price_is_live=? WHERE id=?""",
                (ev["price"], ev["ma21"], stamp, ev["alert_state"], ev["break_date"], ev["note"],
                 ev["target_hit_date"], ev["price_asof"], 1 if ev["price_is_live"] else 0, p["id"]))
            summary["positions"] += 1
            if ev["alert_state"] == "sell":
                summary["sell"].append(p["ticker"])
            elif ev["alert_state"] == "below_ma21":
                summary["warn"].append(p["ticker"])
        # current price on every active signal (shown next to the buy level in the tab)
        for s in armed + watching + triggered:
            bars = bars_by_ticker.get(s["ticker"])
            if bars:
                last = bars[-1]
                conn.execute(
                    "UPDATE hk_surge_signals SET last_price=?, price_asof=?, price_is_live=?, last_checked=? WHERE id=?",
                    (last["close"], last["date"],
                     1 if (last["date"] == clock["today"] and not clock["is_final"]) else 0, stamp, s["id"]))

        for s in armed:
            bars = bars_by_ticker.get(s["ticker"])
            if not bars:
                summary["errors"].append(f"{s['ticker']}: no price data")
                continue
            summary["armed"] += 1
            idx = next((i for i, b in enumerate(bars) if b["date"] == s["surge_date"]), None)
            if idx is None:
                continue
            ev = ss.evaluate_signal(bars, idx)              # only armed -> triggered is accepted live
            if ev["status"] == "triggered":
                conn.execute(
                    """UPDATE hk_surge_signals SET status='triggered', trigger_date=?, window_days_left=NULL,
                           note=?, updated_at=? WHERE id=? AND status='armed'""",
                    (ev["trigger_date"], ss.LIVE_TRIGGER_NOTE, stamp, s["id"]))
                summary["triggered"].append(s["ticker"])

        # Drop Day detection: a "watching" signal whose first red candle (close < open) has now CLOSED becomes
        # "armed" (or "triggered" if the buy level was already reached). Today's still-forming bar is never used.
        for s in watching:
            bars = bars_by_ticker.get(s["ticker"])
            if not bars:
                continue
            summary["watching"] += 1
            forming = bars[-1]["date"] == clock["today"] and not clock["is_final"]
            series = bars[:-1] if forming else bars
            idx = next((i for i, b in enumerate(series) if b["date"] == s["surge_date"]), None)
            if idx is None:
                continue
            ev = ss.evaluate_signal(series, idx)
            if ev["status"] in ("armed", "triggered"):
                conn.execute(
                    """UPDATE hk_surge_signals SET status=?, drop_date=?, buy_level=?, window_days_left=?, trigger_date=?,
                           note=?, updated_at=? WHERE id=? AND status='watching'""",
                    (ev["status"], ev["drop_date"], ev["buy_level"], ev["window_days_left"], ev["trigger_date"],
                     "drop day detected live by the monitor", stamp, s["id"]))
                summary["drop_day"].append(s["ticker"])
        conn.commit()
    finally:
        conn.close()
    return summary


def run_refresh(reason="manual", now=None, bars_by_ticker=None):
    if not _monitor_lock.acquire(blocking=False):
        return None
    MONITOR["running"] = True
    try:
        summary = refresh_live(now=now, bars_by_ticker=bars_by_ticker)
        MONITOR.update(last_run=summary["checked_at"], last_reason=reason, last_error=None,
                       last_summary={k: (len(v) if isinstance(v, list) else v) for k, v in summary.items() if k != "checked_at"})
        if reason != "startup":
            MONITOR["last_run_dt"] = now or datetime.now(HKT)
        return summary
    except Exception as exc:
        MONITOR["last_error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        MONITOR["running"] = False
        _monitor_lock.release()


def monitor_due(now):
    if now.weekday() >= 5:
        return None
    t = now.time()
    if OPEN_CHECK <= t < POST_CLOSE_RUN and not (LUNCH[0] <= t < LUNCH[1]):
        last = MONITOR["last_run_dt"]
        if last is None or now - last >= timedelta(minutes=MONITOR_INTERVAL_MIN):
            return "interval"
    elif POST_CLOSE_RUN <= t < POST_CLOSE_END and MONITOR["post_close_done"] != now.strftime("%Y-%m-%d"):
        return "post-close"
    return None


def next_run_estimate(now=None):
    now = now or datetime.now(HKT)
    if monitor_due(now):
        return now
    t = now.time()
    if now.weekday() < 5:
        if t < OPEN_CHECK:
            return now.replace(hour=OPEN_CHECK.hour, minute=OPEN_CHECK.minute, second=0, microsecond=0)
        if t < POST_CLOSE_RUN:
            last = MONITOR["last_run_dt"]
            nxt = max(now, last + timedelta(minutes=MONITOR_INTERVAL_MIN)) if last else now
            if LUNCH[0] <= nxt.time() < LUNCH[1]:
                nxt = nxt.replace(hour=LUNCH[1].hour, minute=LUNCH[1].minute, second=0, microsecond=0)
            return nxt
    d = now
    for _ in range(4):
        d = d + timedelta(days=1)
        if d.weekday() < 5:
            return d.replace(hour=OPEN_CHECK.hour, minute=OPEN_CHECK.minute, second=0, microsecond=0)
    return None


def _monitor_loop():
    try:
        run_refresh("startup")
    except Exception as exc:
        MONITOR["last_error"] = f"{type(exc).__name__}: {exc}"
    while True:
        try:
            now = datetime.now(HKT)
            reason = monitor_due(now)
            if reason:
                run_refresh(reason)
                if reason == "post-close":
                    MONITOR["post_close_done"] = now.strftime("%Y-%m-%d")
        except Exception as exc:
            MONITOR["last_error"] = f"{type(exc).__name__}: {exc}"
        _time.sleep(30)


def start_monitor_thread():
    """Start the HK live monitor once per process (daemon thread)."""
    if MONITOR["enabled"]:
        return False
    MONITOR["enabled"] = True
    threading.Thread(target=_monitor_loop, daemon=True, name="hk-surge-monitor").start()
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scan stored HK data for Volume Surge signals")
    ap.add_argument("--as-of", help="YYYY-MM-DD - scan as if this were the latest day")
    args = ap.parse_args()
    res = scan_signals(as_of=args.as_of)
    if "error" in res:
        sys.exit(res["error"])
    print(f"as_of={res['as_of']}  tickers={res['tickers_scanned']}  new={len(res['new'])}  "
          f"changed={len(res['changed'])}  filtered_out={res['filtered_out']}  dead_on_arrival={res['dead_on_arrival']}")
    for t in res["new"]:
        print("  NEW", t)
    for t in res["changed"]:
        print("  CHG", t)
