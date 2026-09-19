"""
Volume Surge strategy engine (Scenario A) — the live/dashboard version of the
logic studied in scripts/study_volume_surge_2026_04.py.

Workflow states for a signal (table `surge_signals`):
  watching   surge day found, waiting for the first red candle (drop day)
  armed      drop day found; a limit-buy at the surge-day close is live for
             BUY_WINDOW_DAYS trading days after the drop day
  triggered  a day's High reached the limit price inside that window ("Buy Signal")
  expired    no drop day within MAX_DAYS_TO_DROP days, or the buy window ran out
  bought     user marked it as actually bought (phase 2)
  dismissed  user dismissed it (phase 2)

Rules (all on daily bars):
  Surge day  volume > VOL_MULT * vol_ma10, close > ma200, ma50 > ma200, close > prev close
  Drop day   first SOLID red candle (close < open) strictly after the surge day,
             within MAX_DAYS_TO_DROP trading days (the backtest had no such cap —
             set it to None for exact backtest parity)
  Buy        first day after the drop day, within BUY_WINDOW_DAYS trading days,
             whose intraday High >= surge-day close (fills at the surge close)

Scanning is automatic: each run looks at the last LOOKBACK_DAYS trading days for
new surge days and re-evaluates every signal still watching/armed. Universe =
tracked stocks_daily tickers + the rest of the active S&P500/Russell1000
universe_prices (tracked wins on overlap), same as the Pattern Scanner.

Run standalone:  python surge_strategy.py [--as-of YYYY-MM-DD]
"""
import argparse
import threading
import time as _time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from db_setup import get_connection, setup_database

CONFIG = {
    "VOL_MULT": 3.5,
    "LOOKBACK_DAYS": 20,        # trading days back to look for new surge days
    "MAX_DAYS_TO_DROP": 10,     # max trading days from surge to drop day (None = unlimited)
    "BUY_WINDOW_DAYS": 5,       # trading days after the drop day the limit-buy stays live
    "STALE_AFTER_TRIGGER_DAYS": 3,  # a triggered signal not marked bought this many trading days
                                    # after its trigger day expires (None = never)
    "PARTIAL_TARGET_PCT": 0.15,  # used by the position monitor (phase 3)
    "MA21_WINDOW": 21,
}

ACTIVE_STATUSES = ("watching", "armed", "triggered")   # auto-managed; re-evaluated every scan


def _cfg(config=None):
    cfg = dict(CONFIG)
    if config:
        cfg.update(config)
    return cfg


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_series(conn, as_of=None, since=None):
    """{ticker: [row dicts sorted by date]} — tracked (stocks_daily) unioned with
    the rest of the active universe (universe_prices); tracked wins on overlap.
    `as_of` drops later rows (replay what the scan would have seen on that day);
    `since` limits history to recent rows for a fast live scan."""
    cols = "ticker, date, open, high, low, close, volume, vol_ma10, ma50, ma200"
    where, params = ["close IS NOT NULL"], []
    if as_of:
        where.append("date <= ?")
        params.append(as_of)
    if since:
        where.append("date >= ?")
        params.append(since)
    w = " AND ".join(where)
    rows = conn.execute(
        f"""SELECT {cols} FROM stocks_daily WHERE {w}
            UNION ALL
            SELECT {cols} FROM universe_prices
            WHERE {w}
              AND ticker IN (SELECT ticker FROM ticker_universe WHERE is_active = 1)
              AND ticker NOT IN (SELECT DISTINCT ticker FROM stocks_daily)
            ORDER BY ticker, date""",
        params + params,
    ).fetchall()
    by_ticker = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(dict(r))
    return by_ticker


def add_ma21(series, window=21):
    closes = [r["close"] for r in series]
    for i in range(len(series)):
        if i >= window - 1:
            w = closes[i - window + 1: i + 1]
            series[i]["ma21"] = sum(w) / window if all(c is not None for c in w) else None
        else:
            series[i]["ma21"] = None


# ---------------------------------------------------------------------------
# Pure detection logic (row-dict lists, same shape as the other scanners)
# ---------------------------------------------------------------------------

def is_surge(series, i, cfg):
    if i < 1:
        return False
    r, prev_close = series[i], series[i - 1]["close"]
    if (r["vol_ma10"] is None or r["ma200"] is None or r["ma50"] is None
            or r["close"] is None or prev_close is None or r["volume"] is None):
        return False
    return (r["volume"] > cfg["VOL_MULT"] * r["vol_ma10"]
            and r["close"] > r["ma200"]
            and r["ma50"] > r["ma200"]
            and r["close"] > prev_close)


def find_drop_day(series, surge_idx, max_days):
    """First solid red candle (close < open) after the surge day, within
    max_days trading days (None = no limit). A green candle never counts."""
    end = len(series) if max_days is None else min(len(series), surge_idx + 1 + max_days)
    for i in range(surge_idx + 1, end):
        c, o = series[i]["close"], series[i]["open"]
        if c is not None and o is not None and c < o:
            return i
    return None


def evaluate_signal(series, surge_idx, config=None):
    """Where does this surge stand, given all data in `series`?
    Returns dict(status, drop_date, buy_level, window_days_left, trigger_date, note)."""
    cfg = _cfg(config)
    last = len(series) - 1
    surge_close = series[surge_idx]["close"]
    out = {"status": "watching", "drop_date": None, "buy_level": None,
           "window_days_left": None, "trigger_date": None, "note": None}

    drop_idx = find_drop_day(series, surge_idx, cfg["MAX_DAYS_TO_DROP"])
    if drop_idx is None:
        cap = cfg["MAX_DAYS_TO_DROP"]
        if cap is not None and last >= surge_idx + cap:
            out["status"] = "expired"
            out["note"] = f"no red candle within {cap} trading days of the surge"
        return out

    out["drop_date"] = series[drop_idx]["date"]
    out["buy_level"] = round(surge_close, 4)
    window = cfg["BUY_WINDOW_DAYS"]
    w0 = drop_idx + 1
    for i in range(w0, min(w0 + window, len(series))):
        h = series[i]["high"]
        if h is not None and h >= surge_close:
            out["status"] = "triggered"
            out["trigger_date"] = series[i]["date"]
            stale = cfg["STALE_AFTER_TRIGGER_DAYS"]
            if stale is not None and last - i > stale:
                out["status"] = "expired"
                out["note"] = f"triggered {series[i]['date']}; not marked bought within {stale} trading days"
            return out

    if len(series) >= w0 + window:
        out["status"] = "expired"
        out["note"] = f"High never reached the surge close within {window} trading days of the drop day"
    else:
        out["status"] = "armed"
        out["window_days_left"] = drop_idx + window - last
    return out


def find_surge_indices(series, cfg, start_date=None, end_date=None, last_n=None):
    """Indices of surge days: within [start_date, end_date] (replay) or the last
    `last_n` trading days (live scan)."""
    if last_n is not None:
        rng = range(max(1, len(series) - last_n), len(series))
    else:
        rng = range(1, len(series))
    out = []
    for i in rng:
        d = series[i]["date"]
        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue
        if is_surge(series, i, cfg):
            out.append(i)
    return out


def replay(by_ticker, start_date, end_date, config=None):
    """Evaluate every surge in [start_date, end_date] against all loaded data —
    the backtest-style view, used to verify the engine against the study."""
    cfg = _cfg(config)
    results = []
    for ticker, series in by_ticker.items():
        for i in find_surge_indices(series, cfg, start_date=start_date, end_date=end_date):
            ev = evaluate_signal(series, i, cfg)
            ev.update(ticker=ticker, surge_date=series[i]["date"])
            results.append(ev)
    return results


# ---------------------------------------------------------------------------
# Live scan -> surge_signals
# ---------------------------------------------------------------------------

def scan_signals(as_of=None, config=None):
    """Find new surge days in the last LOOKBACK_DAYS trading days and refresh
    every watching/armed/triggered signal (a triggered one goes stale after
    STALE_AFTER_TRIGGER_DAYS). Idempotent: safe to run as often as you like.
    Signals the user has bought/dismissed, and ones already expired, are never
    touched. Returns a summary dict."""
    cfg = _cfg(config)
    with get_connection() as conn:
        latest = conn.execute("SELECT MAX(date) FROM stocks_daily").fetchone()[0]
        if not latest:
            return {"error": "no price data"}
        ref = min(as_of, latest) if as_of else latest
        # ~90 calendar days comfortably covers LOOKBACK_DAYS + the drop/buy windows
        since = (datetime.strptime(ref, "%Y-%m-%d") - timedelta(days=90)).strftime("%Y-%m-%d")
        by_ticker = load_series(conn, as_of=ref, since=since)

        active = {}
        for r in conn.execute(
                "SELECT ticker, surge_date FROM surge_signals "
                "WHERE status IN ('watching','armed','triggered')"):
            active.setdefault(r["ticker"], set()).add(r["surge_date"])

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        summary = {"as_of": ref, "tickers_scanned": len(by_ticker), "new": [],
                   "changed": [], "dead_on_arrival": 0}

        for ticker, series in by_ticker.items():
            date_idx = {r["date"]: i for i, r in enumerate(series)}
            idxs = set(find_surge_indices(series, cfg, last_n=cfg["LOOKBACK_DAYS"]))
            idxs |= {date_idx[d] for d in active.get(ticker, ()) if d in date_idx}
            for i in sorted(idxs):
                sdate = series[i]["date"]
                ev = evaluate_signal(series, i, cfg)
                row = conn.execute(
                    "SELECT id, status FROM surge_signals WHERE ticker=? AND surge_date=?",
                    (ticker, sdate)).fetchone()
                if row is None:
                    if ev["status"] == "expired":
                        summary["dead_on_arrival"] += 1
                        continue
                    vr = (series[i]["volume"] / series[i]["vol_ma10"]) if series[i]["vol_ma10"] else None
                    conn.execute(
                        """INSERT INTO surge_signals
                           (ticker, surge_date, surge_close, surge_vol_ratio, status, drop_date,
                            buy_level, window_days_left, trigger_date, note, first_seen, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (ticker, sdate, series[i]["close"], round(vr, 2) if vr else None,
                         ev["status"], ev["drop_date"], ev["buy_level"], ev["window_days_left"],
                         ev["trigger_date"], ev["note"], now, now))
                    summary["new"].append((ticker, sdate, ev["status"]))
                elif row["status"] == "triggered" and ev["status"] in ("armed", "watching"):
                    continue   # triggered live by the monitor before the daily bar was stored — never downgrade
                elif row["status"] in ACTIVE_STATUSES:
                    conn.execute(
                        """UPDATE surge_signals SET status=?, drop_date=?, buy_level=?,
                               window_days_left=?, trigger_date=?, note=?, updated_at=?
                           WHERE id=?""",
                        (ev["status"], ev["drop_date"], ev["buy_level"], ev["window_days_left"],
                         ev["trigger_date"], ev["note"], now, row["id"]))
                    if ev["status"] != row["status"]:
                        summary["changed"].append((ticker, sdate, row["status"], ev["status"]))
        conn.commit()
    return summary


# ---------------------------------------------------------------------------
# Live monitor (phase 3): positions marked as bought + armed signals
# ---------------------------------------------------------------------------
# Refreshed every 30 min while the US market is open (plus a post-close pass),
# from Yahoo daily bars (~15 min delayed; today's bar is the still-forming one).
#
# Exit rule (same as the backtest, with one live-safe difference):
#   day i closes below its MA21, and the NEXT trading day's OPEN is still below
#   that MA21  ->  SELL (fill = that next open).
# The backtest compares the next open to the *next* day's MA21, which already
# contains that day's close (unknowable at the open); live uses MA21 as of day i.
# The rule is evaluated statelessly over the bars since the buy date, so a
# confirmed sell is still found if the server was off for a day.

NY = ZoneInfo("America/New_York")
MONITOR_INTERVAL_MIN = 30
MARKET_OPEN_CHECK = dtime(9, 35)     # first pass, once the open print exists
MARKET_CLOSE = dtime(16, 0)
POST_CLOSE_RUN = dtime(16, 20)       # today's bar is final by now
POST_CLOSE_END = dtime(18, 0)

MONITOR = {"enabled": False, "running": False, "last_run": None, "last_run_dt": None,
           "last_reason": None, "last_error": None, "last_summary": None,
           "post_close_done": None}
_monitor_lock = threading.Lock()


def market_clock(now=None):
    """now (ET), today's date, whether the market is open, and whether today's
    daily bar is final (after POST_CLOSE_RUN)."""
    now = now or datetime.now(NY)
    weekday = now.weekday() < 5
    t = now.time()
    return {"now": now, "today": now.strftime("%Y-%m-%d"), "weekday": weekday,
            "is_open": weekday and dtime(9, 30) <= t < MARKET_CLOSE,
            "is_final": t >= POST_CLOSE_RUN}


def _ma(closes, i, window):
    if i < window - 1:
        return None
    w = closes[i - window + 1: i + 1]
    return sum(w) / window if all(c is not None for c in w) else None


def evaluate_position(bars, buy_date, partial_target, half_sold, today, final_today, config=None):
    """Alert state for one open position from daily bars (ascending; the last
    one may be today's still-forming bar).  Returns dict with:
      alert_state  'ok' | 'below_ma21' | 'sell'
      break_date   day whose close broke MA21 (pending or confirmed)
      note         human-readable reason
      price/ma21/price_asof/price_is_live, target_hit_date, sell_date/sell_open"""
    cfg = _cfg(config)
    W = cfg["MA21_WINDOW"]
    out = {"alert_state": "ok", "break_date": None, "note": None, "ma21": None,
           "price": None, "price_asof": None, "price_is_live": False,
           "target_hit_date": None, "sell_date": None, "sell_open": None}
    n = len(bars)
    if n == 0:
        return out
    closes = [b["close"] for b in bars]
    last = bars[-1]
    partial_today = last["date"] == today and not final_today
    out.update(price=last["close"], price_asof=last["date"], price_is_live=partial_today,
               ma21=_ma(closes, n - 1, W))

    start = next((i for i, b in enumerate(bars) if b["date"] > buy_date), None)
    if start is None:                      # nothing after the buy day yet
        return out

    # +15% partial target: a close at/above it (backtest rule); today's forming bar
    # only counts as an intraday touch.
    if not half_sold and partial_target:
        for i in range(start, n):
            c = closes[i]
            if c is not None and c >= partial_target and not (partial_today and i == n - 1):
                out["target_hit_date"] = bars[i]["date"]
                break
        else:
            if partial_today and closes[-1] is not None and closes[-1] >= partial_target:
                out["target_hit_date"] = today

    # confirmed cut-loss: close_i < MA21_i and open_{i+1} < MA21_i
    for i in range(start, n - 1):
        m = _ma(closes, i, W)
        if m is None or closes[i] is None or not closes[i] < m:
            continue
        nxt_open = bars[i + 1]["open"]
        if nxt_open is not None and nxt_open < m:
            out.update(alert_state="sell", break_date=bars[i]["date"],
                       sell_date=bars[i + 1]["date"], sell_open=nxt_open,
                       note=f"closed {closes[i]:.2f} < MA21 {m:.2f} on {bars[i]['date']}, "
                            f"then opened {nxt_open:.2f} on {bars[i + 1]['date']} — SELL")
            return out

    m_now = out["ma21"]
    if m_now is not None and closes[-1] is not None and closes[-1] < m_now:
        if partial_today:
            out.update(alert_state="below_ma21",
                       note=f"trading {closes[-1]:.2f} below MA21 {m_now:.2f} (intraday, unconfirmed)")
        elif n - 1 >= start:
            out.update(alert_state="below_ma21", break_date=last["date"],
                       note=f"closed {closes[-1]:.2f} below MA21 {m_now:.2f} on {last['date']} — "
                            f"sell if the next open is still below")
    return out


def fetch_live_bars(tickers, period="6mo"):
    """{ticker: [ {date, open, high, low, close} ascending ]} from Yahoo daily
    bars (auto-adjusted). The last bar is today's while the market is open."""
    import pandas as pd
    import yfinance as yf
    if not tickers:
        return {}
    df = yf.download(list(tickers), period=period, interval="1d", group_by="ticker",
                     auto_adjust=True, progress=False, threads=True)
    out = {}
    multi = isinstance(df.columns, pd.MultiIndex)
    for t in tickers:
        try:
            sub = df[t] if multi else df
            sub = sub.dropna(subset=["Close"])
        except Exception:
            continue

        def num(v):
            return None if pd.isna(v) else float(v)

        bars = [{"date": idx.strftime("%Y-%m-%d"), "open": num(r["Open"]), "high": num(r["High"]),
                 "low": num(r["Low"]), "close": num(r["Close"])} for idx, r in sub.iterrows()]
        if bars:
            out[t] = bars
    return out


def refresh_live(now=None, bars_by_ticker=None):
    """One live pass: re-evaluate every open position and let armed signals
    trigger intraday. `bars_by_ticker` lets tests inject bars (no network)."""
    clock = market_clock(now)
    stamp = clock["now"].strftime("%Y-%m-%d %H:%M:%S")
    summary = {"checked_at": stamp, "positions": 0, "armed": 0, "triggered": [],
               "sell": [], "warn": [], "errors": []}
    with get_connection() as conn:
        positions = [dict(r) for r in conn.execute(
            "SELECT * FROM surge_positions WHERE status IN ('holding','partial_sold')")]
        armed = [dict(r) for r in conn.execute("SELECT * FROM surge_signals WHERE status='armed'")]
    tickers = sorted({p["ticker"] for p in positions} | {s["ticker"] for s in armed})
    if not tickers:
        return summary
    if bars_by_ticker is None:
        bars_by_ticker = fetch_live_bars(tickers)      # network — outside any DB transaction

    with get_connection() as conn:
        for p in positions:
            bars = bars_by_ticker.get(p["ticker"])
            if not bars:
                summary["errors"].append(f"{p['ticker']}: no price data")
                continue
            ev = evaluate_position(bars, p["buy_date"], p["partial_target"],
                                   p["status"] == "partial_sold", clock["today"], clock["is_final"])
            conn.execute(
                """UPDATE surge_positions SET last_price=?, last_ma21=?, last_checked=?, alert_state=?,
                       ma21_break_date=?, alert_note=?, target_hit_date=?, price_asof=?, price_is_live=?
                   WHERE id=?""",
                (ev["price"], ev["ma21"], stamp, ev["alert_state"], ev["break_date"], ev["note"],
                 ev["target_hit_date"], ev["price_asof"], 1 if ev["price_is_live"] else 0, p["id"]))
            summary["positions"] += 1
            if ev["alert_state"] == "sell":
                summary["sell"].append(p["ticker"])
            elif ev["alert_state"] == "below_ma21":
                summary["warn"].append(p["ticker"])

        for s in armed:
            bars = bars_by_ticker.get(s["ticker"])
            if not bars:
                summary["errors"].append(f"{s['ticker']}: no price data")
                continue
            summary["armed"] += 1
            idx = next((i for i, b in enumerate(bars) if b["date"] == s["surge_date"]), None)
            if idx is None:
                continue
            # only accept armed -> triggered live; expiry stays with the end-of-day scan
            # (today's forming bar would otherwise count as an elapsed window day)
            ev = evaluate_signal(bars, idx)
            if ev["status"] == "triggered":
                conn.execute(
                    """UPDATE surge_signals SET status='triggered', trigger_date=?, window_days_left=NULL,
                           note=?, updated_at=? WHERE id=? AND status='armed'""",
                    (ev["trigger_date"], "triggered live by the monitor", stamp, s["id"]))
                summary["triggered"].append(s["ticker"])
        conn.commit()
    return summary


def run_refresh(reason="manual", now=None, bars_by_ticker=None):
    """Serialised refresh_live() that records status for the UI. Returns the
    summary, or None if a refresh is already running."""
    if not _monitor_lock.acquire(blocking=False):
        return None
    MONITOR["running"] = True
    try:
        summary = refresh_live(now=now, bars_by_ticker=bars_by_ticker)
        MONITOR.update(last_run=summary["checked_at"], last_reason=reason, last_error=None,
                       last_summary={k: (len(v) if isinstance(v, list) else v)
                                     for k, v in summary.items() if k != "checked_at"})
        if reason != "startup":      # a startup pass must not push back the 9:35 open check
            MONITOR["last_run_dt"] = now or datetime.now(NY)
        return summary
    except Exception as exc:
        MONITOR["last_error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        MONITOR["running"] = False
        _monitor_lock.release()


def monitor_due(now):
    """Why a live pass should run right now ('interval' / 'post-close'), or None."""
    if now.weekday() >= 5:
        return None
    t = now.time()
    if MARKET_OPEN_CHECK <= t < POST_CLOSE_RUN:
        last = MONITOR["last_run_dt"]
        if last is None or now - last >= timedelta(minutes=MONITOR_INTERVAL_MIN):
            return "interval"
    elif POST_CLOSE_RUN <= t < POST_CLOSE_END and MONITOR["post_close_done"] != now.strftime("%Y-%m-%d"):
        return "post-close"
    return None


def next_run_estimate(now=None):
    """Approximate time (ET) of the next automatic pass."""
    now = now or datetime.now(NY)
    if monitor_due(now):
        return now
    t = now.time()
    if now.weekday() < 5:
        if t < MARKET_OPEN_CHECK:
            return now.replace(hour=MARKET_OPEN_CHECK.hour, minute=MARKET_OPEN_CHECK.minute, second=0, microsecond=0)
        if t < POST_CLOSE_RUN:
            last = MONITOR["last_run_dt"]
            return max(now, last + timedelta(minutes=MONITOR_INTERVAL_MIN)) if last else now
    d = now
    for _ in range(4):
        d = d + timedelta(days=1)
        if d.weekday() < 5:
            return d.replace(hour=MARKET_OPEN_CHECK.hour, minute=MARKET_OPEN_CHECK.minute, second=0, microsecond=0)
    return None


def _monitor_loop():
    try:                                              # one pass at startup so the board isn't stale
        run_refresh("startup")
    except Exception as exc:
        MONITOR["last_error"] = f"{type(exc).__name__}: {exc}"
    while True:
        try:
            now = datetime.now(NY)
            reason = monitor_due(now)
            if reason:
                run_refresh(reason)
                if reason == "post-close":
                    MONITOR["post_close_done"] = now.strftime("%Y-%m-%d")
        except Exception as exc:                      # keep the thread alive; surfaced in status
            MONITOR["last_error"] = f"{type(exc).__name__}: {exc}"
        _time.sleep(30)


def start_monitor_thread():
    """Start the 30-min live monitor once per process (daemon thread)."""
    if MONITOR["enabled"]:
        return False
    MONITOR["enabled"] = True
    threading.Thread(target=_monitor_loop, daemon=True, name="surge-monitor").start()
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scan for Volume Surge strategy signals")
    ap.add_argument("--as-of", help="YYYY-MM-DD — scan as if this were the latest day")
    args = ap.parse_args()
    setup_database()
    res = scan_signals(as_of=args.as_of)
    print(f"as_of={res.get('as_of')}  tickers={res.get('tickers_scanned')}  "
          f"new={len(res.get('new', []))}  changed={len(res.get('changed', []))}  "
          f"dead_on_arrival={res.get('dead_on_arrival')}")
    for t in res.get("new", []):
        print("  NEW", t)
    for t in res.get("changed", []):
        print("  CHG", t)
