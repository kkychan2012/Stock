"""
intraday_monitor.py — hourly intraday monitor during US market hours.

Two parts, both run once an hour (10:00 ET first, every 60 min to the close,
plus one post-close pass at 16:20 ET) while api_server.py is running:

  1. FOCUS LIST (per-ticker daily bars, ~1 request per ticker)
     Monitor-tab watchlists + the latest Morning Brief's 10 picks + Holdings.
     Alerts: Monitor target range hit (same rule as the Monitor tab's
     "Refresh Prices" — sets monitor_list.range_hit), Morning Brief stop /
     resistance crossed, trading below MA21 (was above yesterday), new 30-day
     or 52-week high, big move (+/-5%), heavy volume pace.

  2. WHOLE MARKET (Yahoo batch quote endpoint, 250 symbols per request, so the
     ~1,100-stock universe costs ~5 requests per pass)
     Every active S&P 500 / Russell 1000 stock + tracked US tickers, liquid
     names only (price >= $5, >= $10M/day). Alerts: breaking the 52-week high
     on volume, possible Surge-strategy day forming (up, above MA200, MA50 >
     MA200, volume pace >= the strategy's VOL_MULT), big move +/-5% on volume,
     reclaiming MA200, a Trend-Template leader losing MA50.

"Volume pace" = today's volume so far / (10-day average volume x fraction of
the session elapsed). Linear, so it overstates the first hour (volume is
front-loaded) — which is why the first pass waits until 10:00 ET.

Prior-day context (52-wk high, MA50/200, Vol MA10, trend score) comes from the
stored daily data (stocks_daily, falling back to universe_prices), i.e. the
last Fetch / quick update. Quotes are ~15 min delayed.

Alerts go to intraday_alerts, one row per (trade_date, ticker, alert_type);
a later pass updates price/last_seen but keeps first_seen.

CLI (one pass now, ignores market hours):  python intraday_monitor.py [--market-only|--focus-only]
"""

import argparse
import json
import threading
import time as _time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from db_setup import get_connection, setup_database

NY = ZoneInfo("America/New_York")
INTERVAL_MIN = 60
FIRST_PASS = dtime(10, 0)
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
POST_CLOSE_RUN = dtime(16, 20)
POST_CLOSE_END = dtime(18, 0)

QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
QUOTE_BATCH = 250
MIN_PRICE = 5.0
MIN_DOLLAR_VOL = 10e6
BIG_MOVE_PCT = 5.0
PACE_HIGH = 1.5           # "on volume"
PACE_HEAVY = 2.5          # focus-list heavy-volume alert
# Store today's still-forming daily bar (built from the batch quotes) into stocks_daily /
# universe_prices on every whole-market pass, recompute indicators / RS / Trend Template for
# today and re-run the pattern scan for today -> Signals, Pattern Scanner and Data tabs show
# the current price. The Nightly Scan's normal quick update overwrites the bar with the final one.
STORE_INTRADAY_BARS = True
SCAN_PATTERNS_INTRADAY = True
BAR_STORE_BLOCKER = None  # api_server sets a callable -> True while a Fetch / quick update runs
MA21_WINDOW = 21

LABELS = {
    "range_hit": "Target range hit",
    "pick_stop": "Brief pick below its stop",
    "pick_breakout": "Brief pick above resistance",
    "below_ma21": "Below MA21",
    "new_high_30d": "New 30-day high",
    "new_high_52wk": "New 52-week high",
    "big_up": "Big move up",
    "big_down": "Big move down",
    "heavy_volume": "Heavy volume",
    "surge_forming": "Possible Surge day",
    "ma200_reclaim": "Reclaiming MA200",
    "ma50_lost": "Leader losing MA50",
}

STATE = {"enabled": False, "running": False, "last_run": None, "last_run_dt": None,
         "last_reason": None, "last_error": None, "last_summary": None, "post_close_done": None}
_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────────────────────

def focus_tickers(conn):
    """{ticker: [sources]} — watchlists, latest brief picks, holdings."""
    out = {}

    def add(t, src):
        if t and "." not in t and not t.startswith("^"):
            out.setdefault(t.upper(), [])
            if src not in out[t.upper()]:
                out[t.upper()].append(src)

    for (t,) in conn.execute("SELECT DISTINCT ticker FROM monitor_list"):
        add(t, "watchlist")
    last = conn.execute("SELECT MAX(as_of) FROM daily_report_picks").fetchone()[0]
    if last:
        for (t,) in conn.execute("SELECT ticker FROM daily_report_picks WHERE as_of=?", (last,)):
            add(t, "brief")
    for (t,) in conn.execute("SELECT DISTINCT ticker FROM holdings"):
        add(t, "holding")
    return out


def market_tickers(conn):
    ts = {r[0] for r in conn.execute("SELECT ticker FROM ticker_universe WHERE is_active=1")}
    ts |= {r[0] for r in conn.execute("SELECT ticker FROM extraction_tickers")}
    return sorted(t for t in ts if t and "." not in t and not t.startswith("^"))


def prior_context(conn, before_date):
    """Latest stored row strictly before `before_date` per ticker (tracked wins)."""
    cols = "ticker, date, close, high_30d, high_52wk, ma50, ma200, vol_ma10, trend_score"
    ctx = {}
    for table in ("universe_prices", "stocks_daily"):        # stocks_daily second -> wins
        q = (f"SELECT {cols} FROM {table} t WHERE date = (SELECT MAX(date) FROM {table} "
             f"WHERE ticker = t.ticker AND date < ?)")
        for r in conn.execute(q, (before_date,)):
            ctx[r["ticker"]] = dict(r)
    return ctx


def fetch_quotes(symbols):
    """{symbol: quote dict} from Yahoo's batch quote endpoint (yfinance handles cookie/crumb)."""
    from yfinance.data import YfData
    d = YfData()
    out, errors = {}, []
    for i in range(0, len(symbols), QUOTE_BATCH):
        part = symbols[i:i + QUOTE_BATCH]
        try:
            r = d.get(QUOTE_URL, params={"symbols": ",".join(part)})
            if r.status_code != 200:
                errors.append(f"quote batch {i // QUOTE_BATCH + 1}: HTTP {r.status_code}")
                continue
            for q in (r.json().get("quoteResponse") or {}).get("result") or []:
                if q.get("symbol"):
                    out[q["symbol"]] = q
        except Exception as exc:
            errors.append(f"quote batch {i // QUOTE_BATCH + 1}: {type(exc).__name__}: {exc}")
        _time.sleep(0.5)
    return out, errors


def _live_rows(quotes):
    """Batch-quote dicts (already limited to today's session) -> live_quotes rows."""
    rows = []
    for t, q in quotes.items():
        price = q.get("regularMarketPrice")
        if price is None or not q.get("regularMarketTime"):
            continue
        ts = datetime.fromtimestamp(q["regularMarketTime"], NY)
        rows.append((t, ts.strftime("%Y-%m-%d"), price, q.get("regularMarketPreviousClose"),
                     q.get("regularMarketChangePercent"), q.get("regularMarketVolume"),
                     ts.strftime("%Y-%m-%d %H:%M"), q.get("marketState")))
    return rows


def save_live_quotes(conn, rows, stamp):
    conn.executemany(
        "INSERT INTO live_quotes (ticker, trade_date, price, prev_close, change_pct, volume, quote_time, "
        "market_state, updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET "
        "trade_date=excluded.trade_date, price=excluded.price, prev_close=excluded.prev_close, "
        "change_pct=excluded.change_pct, volume=excluded.volume, quote_time=excluded.quote_time, "
        "market_state=excluded.market_state, updated_at=excluded.updated_at",
        [r + (stamp,) for r in rows])


def get_live_quotes():
    """{trade_date, updated_at, quotes: {T: {...}}} — the latest session's quotes only."""
    with get_connection() as conn:
        last = conn.execute("SELECT MAX(trade_date) FROM live_quotes").fetchone()[0]
        if not last:
            return {"trade_date": None, "updated_at": None, "quotes": {}}
        rows = conn.execute("SELECT * FROM live_quotes WHERE trade_date=?", (last,)).fetchall()
    return {"trade_date": last, "updated_at": max((r["updated_at"] or "") for r in rows) or None,
            "quotes": {r["ticker"]: {"price": r["price"], "prev_close": r["prev_close"],
                                     "change_pct": r["change_pct"], "volume": r["volume"],
                                     "time": r["quote_time"], "state": r["market_state"]} for r in rows}}


def build_intraday_bars(quotes, today):
    """Batch quotes -> {ticker: one-row DataFrame (today's forming bar)}; incomplete quotes skipped."""
    import pandas as pd
    ts, out = pd.Timestamp(today), {}
    for t, q in quotes.items():
        o, h, l, c = (q.get(k) for k in ("regularMarketOpen", "regularMarketDayHigh",
                                         "regularMarketDayLow", "regularMarketPrice"))
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            continue
        out[t] = pd.DataFrame({"Open": [float(o)], "High": [float(max(h, o, c))], "Low": [float(min(l, o, c))],
                               "Close": [float(c)], "Volume": [float(q.get("regularMarketVolume") or 0)]},
                              index=[ts])
    return out


def store_intraday_bars(quotes, today, summary):
    """Write today's forming bars + recompute today's indicators/RS/Trend Template, then rescan."""
    if not STORE_INTRADAY_BARS:
        return
    if BAR_STORE_BLOCKER and BAR_STORE_BLOCKER():
        summary["bars"] = "skipped (a Fetch / quick update is running)"
        return
    t0 = _time.time()
    try:
        import fetch_data
        import rs_calculator
        bars = build_intraday_bars(quotes, today)
        bq, _ = fetch_quotes([rs_calculator.RS_BENCHMARK])           # RS Line needs the index too
        bq = {t: q for t, q in bq.items() if q.get("regularMarketTime") and
              datetime.fromtimestamp(q["regularMarketTime"], NY).strftime("%Y-%m-%d") == today}
        bars.update(build_intraday_bars(bq, today))
        if not bars:
            summary["bars"] = "no complete quotes"
            return
        st = fetch_data.quick_update(progress_cb=lambda m: None, bars=bars)
        summary["bars"] = {"stored": len(bars), "tracked_updated": st.get("tracked_updated"),
                           "seconds": round(_time.time() - t0, 1)}
        if SCAN_PATTERNS_INTRADAY:
            import scan_patterns
            t1 = _time.time()
            scan_patterns.scan_all_patterns(today)
            with get_connection() as conn:
                n = conn.execute("SELECT COUNT(*) FROM pattern_scan_results WHERE scan_date=?",
                                 (today,)).fetchone()[0]
            summary["pattern_scan"] = {"hits": n, "seconds": round(_time.time() - t1, 1)}
    except Exception as exc:
        summary["errors"].append(f"intraday bars: {type(exc).__name__}: {exc}")


def fetch_bars(tickers):
    """Daily bars (1y, with volume) per ticker — one Yahoo request per ticker."""
    import pandas as pd
    import yfinance as yf
    if not tickers:
        return {}
    df = yf.download(list(tickers), period="1y", interval="1d", group_by="ticker",
                     auto_adjust=True, progress=False, threads=True)
    multi = isinstance(df.columns, pd.MultiIndex)
    out = {}
    for t in tickers:
        try:
            sub = (df[t] if multi else df).dropna(subset=["Close"])
        except Exception:
            continue
        num = lambda v: None if pd.isna(v) else float(v)
        bars = [{"date": idx.strftime("%Y-%m-%d"), "open": num(r["Open"]), "high": num(r["High"]),
                 "low": num(r["Low"]), "close": num(r["Close"]), "volume": num(r["Volume"])}
                for idx, r in sub.iterrows()]
        if bars:
            out[t] = bars
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Rules (pure — no network, no DB)
# ─────────────────────────────────────────────────────────────────────────────

def session_fraction(now):
    """Share of the regular session elapsed at `now` (ET), clamped to [0.05, 1]."""
    if now.weekday() >= 5:
        return 1.0
    mins = (now.hour * 60 + now.minute) - (MARKET_OPEN.hour * 60 + MARKET_OPEN.minute)
    return min(1.0, max(0.05, mins / 390.0))


def vol_pace(volume, avg_volume, frac):
    if not volume or not avg_volume or avg_volume <= 0:
        return None
    return volume / (avg_volume * frac)


def _alert(ticker, typ, scope, price, chg, pace, level, detail):
    return {"ticker": ticker, "alert_type": typ, "scope": scope, "price": price,
            "change_pct": chg, "vol_pace": pace, "level": level, "detail": detail}


def market_alerts(quotes, ctx, frac, vol_mult):
    """Whole-market rules on batch quotes + prior-day stored context."""
    out = []
    for t, q in quotes.items():
        c = ctx.get(t)
        p = q.get("regularMarketPrice")
        if not c or not p or not c.get("close"):
            continue
        avg = c.get("vol_ma10") or q.get("averageDailyVolume10Day")
        if p < MIN_PRICE or not avg or avg * p < MIN_DOLLAR_VOL:
            continue
        prev = c["close"]
        chg = (p / prev - 1) * 100
        pace = vol_pace(q.get("regularMarketVolume"), avg, frac)
        on_vol = pace is not None and pace >= PACE_HIGH
        ma50, ma200, h52 = c.get("ma50"), c.get("ma200"), c.get("high_52wk")
        ps = f"vol pace {pace:.1f}x" if pace else "vol pace n/a"
        if h52 and p > h52 and on_vol:
            out.append(_alert(t, "new_high_52wk", "market", p, chg, pace, h52,
                              f"{p:,.2f} above the 52-wk high {h52:,.2f}, {ps}"))
        if (chg > 0 and ma50 and ma200 and p > ma200 and ma50 > ma200
                and pace is not None and pace >= vol_mult):
            out.append(_alert(t, "surge_forming", "market", p, chg, pace, None,
                              f"up {chg:.1f}% at {ps} (strategy needs {vol_mult:g}x at the close) — "
                              f"confirmed only by tonight's scan"))
        if chg >= BIG_MOVE_PCT and on_vol:
            out.append(_alert(t, "big_up", "market", p, chg, pace, None, f"up {chg:.1f}%, {ps}"))
        elif chg <= -BIG_MOVE_PCT and on_vol:
            out.append(_alert(t, "big_down", "market", p, chg, pace, None, f"down {chg:.1f}%, {ps}"))
        if ma200 and prev < ma200 < p and pace is not None and pace >= 1.2:
            out.append(_alert(t, "ma200_reclaim", "market", p, chg, pace, ma200,
                              f"back above MA200 {ma200:,.2f} (closed {prev:,.2f} below it), {ps}"))
        if (c.get("trend_score") or 0) >= 6 and ma50 and prev >= ma50 and p < ma50 * 0.99:
            out.append(_alert(t, "ma50_lost", "market", p, chg, pace, ma50,
                              f"Trend Template {c['trend_score']}/8 stock trading below MA50 {ma50:,.2f}, {ps}"))
    return out


def focus_alerts(ticker, bars, today, frac, pick=None, sources=()):
    """Focus-list rules on daily bars (last bar = today's, possibly still forming)."""
    out = []
    if not bars or bars[-1]["date"] != today or len(bars) < 2:
        return out
    hist, cur = bars[:-1], bars[-1]
    p, prev = cur["close"], hist[-1]["close"]
    if not p or not prev:
        return out
    chg = (p / prev - 1) * 100
    vols = [b["volume"] for b in hist[-10:] if b["volume"]]
    avg = sum(vols) / len(vols) if vols else None
    pace = vol_pace(cur["volume"], avg, frac)
    ps = f"vol pace {pace:.1f}x" if pace else "vol pace n/a"
    a = lambda typ, lvl, det: out.append(_alert(ticker, typ, "focus", p, chg, pace, lvl, det))

    if pick:
        if pick.get("stop_level") and p < pick["stop_level"]:
            a("pick_stop", pick["stop_level"],
              f"{p:,.2f} below the brief's invalidation level {pick['stop_level']:,.2f} ({pick['as_of']} pick)")
        if pick.get("resistance_level") and p > pick["resistance_level"] >= prev * 0.98:
            a("pick_breakout", pick["resistance_level"],
              f"through first resistance {pick['resistance_level']:,.2f} ({pick['as_of']} pick), {ps}")

    closes = [b["close"] for b in bars]
    if len(closes) > MA21_WINDOW and all(closes[-MA21_WINDOW - 1:]):
        ma_today = sum(closes[-MA21_WINDOW:]) / MA21_WINDOW
        ma_prev = sum(closes[-MA21_WINDOW - 1:-1]) / MA21_WINDOW
        if p < ma_today and prev >= ma_prev:
            a("below_ma21", ma_today, f"{p:,.2f} below MA21 {ma_today:,.2f} (closed above it yesterday)")

    h30 = max((b["high"] for b in hist[-30:] if b["high"]), default=None)
    h52 = max((b["high"] for b in hist[-252:] if b["high"]), default=None)
    if h52 and p > h52:
        a("new_high_52wk", h52, f"{p:,.2f} above the 52-wk high {h52:,.2f}, {ps}")
    elif h30 and p > h30:
        a("new_high_30d", h30, f"{p:,.2f} above the 30-day high {h30:,.2f}, {ps}")
    if chg >= BIG_MOVE_PCT:
        a("big_up", None, f"up {chg:.1f}%, {ps}")
    elif chg <= -BIG_MOVE_PCT:
        a("big_down", None, f"down {chg:.1f}%, {ps}")
    if pace is not None and pace >= PACE_HEAVY:
        a("heavy_volume", None, f"{ps} with the price {chg:+.1f}%")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# One pass
# ─────────────────────────────────────────────────────────────────────────────

def apply_monitor_ranges(conn, prices, stamp):
    """Monitor-tab target range check (same rule as POST /api/monitor/refresh):
    updates last_price, sets range_hit the first time a price lands in range.
    Returns (rows_updated, newly_hit list)."""
    rows_updated, newly_hit = 0, []
    for ticker, price in prices.items():
        if price is None:
            continue
        for r in conn.execute("SELECT id, range_low, range_high, range_hit FROM monitor_list WHERE ticker=?",
                              (ticker,)).fetchall():
            conn.execute("UPDATE monitor_list SET last_price=?, last_checked=? WHERE id=?", (price, stamp, r["id"]))
            rows_updated += 1
            if (not r["range_hit"] and r["range_low"] is not None and r["range_high"] is not None
                    and r["range_low"] <= price <= r["range_high"]):
                conn.execute("UPDATE monitor_list SET range_hit=1, range_hit_at=? WHERE id=?", (stamp, r["id"]))
                newly_hit.append({"id": r["id"], "ticker": ticker, "price": price,
                                  "low": r["range_low"], "high": r["range_high"]})
    return rows_updated, newly_hit


def _save(conn, trade_date, alerts, stamp):
    for x in alerts:
        conn.execute(
            """INSERT INTO intraday_alerts (trade_date, ticker, alert_type, scope, price, change_pct, vol_pace,
                   level, detail, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(trade_date, ticker, alert_type) DO UPDATE SET price=excluded.price,
                   change_pct=excluded.change_pct, vol_pace=excluded.vol_pace, level=excluded.level,
                   detail=excluded.detail, last_seen=excluded.last_seen,
                   scope=CASE WHEN intraday_alerts.scope='focus' THEN 'focus' ELSE excluded.scope END""",
            (trade_date, x["ticker"], x["alert_type"], x["scope"], x["price"], x["change_pct"], x["vol_pace"],
             x["level"], x["detail"], stamp, stamp))


def run_pass(now=None, do_focus=True, do_market=True):
    now = now or datetime.now(NY)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    frac = session_fraction(now)
    summary = {"checked_at": stamp, "trade_date": today, "focus_tickers": 0, "market_quotes": 0,
               "alerts": 0, "focus_alerts": 0, "market_alerts": 0, "range_hits": [], "errors": []}
    try:
        import surge_strategy
        vol_mult = surge_strategy.CONFIG.get("VOL_MULT", 3.5)
    except Exception:
        vol_mult = 3.5

    with get_connection() as conn:
        focus = focus_tickers(conn)
        mkt = market_tickers(conn)
        ctx = prior_context(conn, today)
        last = conn.execute("SELECT MAX(as_of) FROM daily_report_picks").fetchone()[0]
        picks = {r["ticker"]: dict(r) for r in conn.execute(
            "SELECT ticker, as_of, stop_level, support_level, resistance_level FROM daily_report_picks "
            "WHERE as_of=?", (last,))} if last else {}

    alerts, prices = [], {}
    if do_focus and focus:
        summary["focus_tickers"] = len(focus)
        try:
            bars = fetch_bars(sorted(focus))
        except Exception as exc:
            bars = {}
            summary["errors"].append(f"focus bars: {type(exc).__name__}: {exc}")
        for t, src in focus.items():
            b = bars.get(t)
            if not b:
                continue
            if b[-1]["date"] == today:
                prices[t] = b[-1]["close"]
            fa = focus_alerts(t, b, today, frac, picks.get(t), src)
            for x in fa:
                x["detail"] += f"  [{', '.join(src)}]"
            alerts += fa
        summary["focus_alerts"] = len(alerts)

    quotes = {}
    if do_market:
        quotes, errs = fetch_quotes(mkt)
        summary["errors"] += errs
        summary["market_quotes"] = len(quotes)
        # only today's quotes (a holiday/weekend quote is yesterday's close)
        quotes = {t: q for t, q in quotes.items()
                  if q.get("regularMarketTime")
                  and datetime.fromtimestamp(q["regularMarketTime"], NY).strftime("%Y-%m-%d") == today}
        live_rows = _live_rows(quotes)
        ma = market_alerts(quotes, ctx, frac, vol_mult)
        focus_keys = {(x["ticker"], x["alert_type"]) for x in alerts}
        ma = [x for x in ma if (x["ticker"], x["alert_type"]) not in focus_keys]
        for x in ma:
            if x["ticker"] in focus:
                x["scope"] = "focus"
        summary["market_alerts"] = len(ma)
        alerts += ma
        for t in focus:                       # fall back to quote price for range checks
            if t not in prices and t in quotes:
                prices[t] = quotes[t].get("regularMarketPrice")

    with get_connection() as conn:
        if do_market:
            save_live_quotes(conn, live_rows, stamp)
            summary["live_quotes"] = len(live_rows)
        _, hits = apply_monitor_ranges(conn, prices, stamp)
        for h in hits:
            alerts.append(_alert(h["ticker"], "range_hit", "focus", h["price"], None, None, h["low"],
                                 f"{h['price']:,.2f} inside the Monitor target range "
                                 f"{h['low']:,.2f}–{h['high']:,.2f}"))
        _save(conn, today, alerts, stamp)
        conn.commit()
    summary["range_hits"] = [h["ticker"] for h in hits]
    summary["alerts"] = len(alerts)
    if do_market and quotes:
        store_intraday_bars(quotes, today, summary)
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler (daemon thread started by api_server.py)
# ─────────────────────────────────────────────────────────────────────────────

def run_refresh(reason="manual", now=None, **kw):
    if not _lock.acquire(blocking=False):
        return None
    STATE["running"] = True
    try:
        summary = run_pass(now=now, **kw)
        STATE.update(last_run=summary["checked_at"], last_reason=reason, last_summary=summary,
                     last_error="; ".join(summary["errors"][:3]) or None)
        STATE["last_run_dt"] = now or datetime.now(NY)
        return summary
    except Exception as exc:
        STATE["last_error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        STATE["running"] = False
        _lock.release()


def due(now):
    if now.weekday() >= 5:
        return None
    t = now.time()
    if FIRST_PASS <= t < POST_CLOSE_RUN:
        last = STATE["last_run_dt"]
        if last is None or last.date() != now.date() or now - last >= timedelta(minutes=INTERVAL_MIN):
            return "hourly"
    elif POST_CLOSE_RUN <= t < POST_CLOSE_END and STATE["post_close_done"] != now.strftime("%Y-%m-%d"):
        return "post-close"
    return None


def next_run_estimate(now=None):
    now = now or datetime.now(NY)
    if due(now):
        return now
    if now.weekday() < 5:
        t = now.time()
        if t < FIRST_PASS:
            return now.replace(hour=FIRST_PASS.hour, minute=FIRST_PASS.minute, second=0, microsecond=0)
        if t < POST_CLOSE_RUN and STATE["last_run_dt"]:
            return min(STATE["last_run_dt"] + timedelta(minutes=INTERVAL_MIN),
                       now.replace(hour=POST_CLOSE_RUN.hour, minute=POST_CLOSE_RUN.minute, second=0))
    d = now
    for _ in range(4):
        d += timedelta(days=1)
        if d.weekday() < 5:
            return d.replace(hour=FIRST_PASS.hour, minute=FIRST_PASS.minute, second=0, microsecond=0)
    return None


def _loop():
    while True:
        try:
            now = datetime.now(NY)
            reason = due(now)
            if reason:
                run_refresh(reason)
                if reason == "post-close":
                    STATE["post_close_done"] = now.strftime("%Y-%m-%d")
        except Exception as exc:
            STATE["last_error"] = f"{type(exc).__name__}: {exc}"
        _time.sleep(30)


def start_monitor_thread():
    if STATE["enabled"]:
        return False
    STATE["enabled"] = True
    threading.Thread(target=_loop, daemon=True, name="intraday-monitor").start()
    return True


def status(now=None):
    now = now or datetime.now(NY)
    t = now.time()
    s = {k: v for k, v in STATE.items() if k not in ("last_run_dt", "last_summary")}
    s["summary"] = STATE["last_summary"]
    s["market_open"] = now.weekday() < 5 and MARKET_OPEN <= t < MARKET_CLOSE
    nr = next_run_estimate(now)
    s["next_run"] = nr.strftime("%Y-%m-%d %H:%M") if nr else None
    s["interval_min"] = INTERVAL_MIN
    return s


def get_alerts(trade_date=None, scope=None):
    with get_connection() as conn:
        if not trade_date:
            trade_date = conn.execute("SELECT MAX(trade_date) FROM intraday_alerts").fetchone()[0]
        if not trade_date:
            return None, []
        q, args = "SELECT * FROM intraday_alerts WHERE trade_date=?", [trade_date]
        if scope:
            q += " AND scope=?"
            args.append(scope)
        rows = [dict(r) for r in conn.execute(q + " ORDER BY first_seen DESC, ticker", args)]
    for r in rows:
        r["label"] = LABELS.get(r["alert_type"], r["alert_type"])
    return trade_date, rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="One intraday monitor pass now")
    ap.add_argument("--market-only", action="store_true")
    ap.add_argument("--focus-only", action="store_true")
    a = ap.parse_args()
    setup_database()
    s = run_pass(do_focus=not a.market_only, do_market=not a.focus_only)
    print(json.dumps(s, indent=2))
