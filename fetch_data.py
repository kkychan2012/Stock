"""
Headless daily fetcher.

Run:  python fetch_data.py
      python fetch_data.py --tickers AAPL MSFT   # override ticker list
      python fetch_data.py --period 1y            # default is 2y
"""

import argparse
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yfinance as yf

from db_setup import get_connection, setup_database, DB_PATH
import universe
import rs_calculator


def _nb(cond: pd.Series, *deps) -> pd.Series:
    """Nullable boolean: 1.0/0.0 where all deps are non-NaN, NaN otherwise.
    Stored as float so _safe_int converts NaN → NULL in SQLite."""
    valid = pd.Series(True, index=cond.index)
    for dep in deps:
        valid &= dep.notna()
    return cond.astype("float64").where(valid)


# ---------------------------------------------------------------------------
# Indicator calculation (mirrors Stock_Figure_Extract_GUI.py logic)
# ---------------------------------------------------------------------------

def _calculate_indicators(data: pd.DataFrame) -> pd.DataFrame:
    data = data.copy()
    data.index = pd.to_datetime(data.index)
    if data.index.tz is not None:
        data.index = data.index.tz_localize(None)

    data["High_30D"]    = data["High"].rolling(window=30).max()
    data["Low_30D"]     = data["Low"].rolling(window=30).min()
    data["MA6"]         = data["Close"].rolling(window=6).mean()
    data["MA10"]        = data["Close"].rolling(window=10).mean()
    data["MA30"]        = data["Close"].rolling(window=30).mean()
    data["MA50"]        = data["Close"].rolling(window=50).mean()
    data["MA100"]       = data["Close"].rolling(window=100).mean()
    data["MA150"]       = data["Close"].rolling(window=150).mean()
    data["MA200"]       = data["Close"].rolling(window=200).mean()
    data["Vol_MA10"]    = data["Volume"].rolling(window=10).mean()
    data["Price_Change"] = data["Close"].diff()
    data["Pct_Change"]  = data["Close"].pct_change() * 100
    data["Direction"]   = data["Price_Change"].apply(
        lambda x: "Up" if x > 0 else ("Down" if x < 0 else "No Change")
    )
    delta    = data["Close"].diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs       = avg_gain / avg_loss
    data["RSI14"] = 100 - (100 / (1 + rs))

    # ── 52-week range ─────────────────────────────────────────────────
    data["High_52wk"] = data["Close"].rolling(window=252, min_periods=1).max()
    data["Low_52wk"]  = data["Close"].rolling(window=252, min_periods=1).min()

    # ── All-time low (point-in-time, bounded by however much history is in
    # the DB) — cumulative min of the daily Low from the earliest fetched
    # row, used by the "Price >= Nx Low" signal to flag the exact day a
    # stock's close first crosses N times whatever its low had been so far.
    data["Low_AllTime"] = data["Low"].cummin()

    # Date that running low was actually set on. Pandas has no built-in
    # "cumulative argmin" over an expanding window, so this is done with
    # numpy: find every row that set a new record low, then for each row
    # look up the most recent such record via searchsorted.
    low_vals = data["Low"].to_numpy()
    running_min = np.minimum.accumulate(low_vals)
    is_record = np.ones(len(low_vals), dtype=bool)
    is_record[1:] = running_min[1:] < running_min[:-1]
    record_pos = np.flatnonzero(is_record)
    last_record_pos = record_pos[np.searchsorted(record_pos, np.arange(len(low_vals)), side="right") - 1]
    data["Low_AllTime_Date"] = data.index[last_record_pos]

    # ── RS raw: weighted price performance (IBD-style approximation) ──
    p63  = data["Close"].pct_change(63)
    p126 = data["Close"].pct_change(126)
    p189 = data["Close"].pct_change(189)
    p252 = data["Close"].pct_change(252)
    w63, w126, w189, w252 = 0.4, 0.2, 0.2, 0.2
    avail_w = (
        p63.notna().astype(float)  * w63  +
        p126.notna().astype(float) * w126 +
        p189.notna().astype(float) * w189 +
        p252.notna().astype(float) * w252
    )
    rs_num = (
        p63.fillna(0)  * w63  +
        p126.fillna(0) * w126 +
        p189.fillna(0) * w189 +
        p252.fillna(0) * w252
    )
    data["RS_raw"] = (rs_num / avail_w).where(avail_w > 0)

    # ── Trend Template criteria C1-C6, C8 (C7 needs cross-ticker RS rank) ──
    ma200_lag21 = data["MA200"].shift(21)
    data["C1"] = _nb(
        (data["Close"] > data["MA150"]) & (data["Close"] > data["MA200"]),
        data["MA150"], data["MA200"],
    )
    data["C2"] = _nb(data["MA150"] > data["MA200"], data["MA150"], data["MA200"])
    data["C3"] = _nb(data["MA200"] > ma200_lag21, data["MA200"], ma200_lag21)
    data["C4"] = _nb(
        (data["MA50"] > data["MA150"]) & (data["MA50"] > data["MA200"]),
        data["MA50"], data["MA150"], data["MA200"],
    )
    data["C5"] = _nb(data["Close"] >= 1.25 * data["Low_52wk"],  data["Low_52wk"])
    data["C6"] = _nb(data["Close"] >= 0.75 * data["High_52wk"], data["High_52wk"])
    data["C8"] = _nb(data["Close"] > data["MA50"], data["MA50"])

    return data


# ---------------------------------------------------------------------------
# Signal detection (Price>MA200, MA10>MA30) on the latest available row
# ---------------------------------------------------------------------------

def _detect_signals(ticker: str, data: pd.DataFrame) -> list[dict]:
    """Return one signal dict per (date, type) where the condition is met.
    Used only to summarise which signals are active on the latest row for the
    fetch progress log — signals themselves are computed live from
    stocks_daily by the /api/signals/* endpoints, not stored separately.
    """
    signals = []
    for date, row in data.iterrows():
        close = row.get("Close")
        if close is None or (isinstance(close, float) and pd.isna(close)):
            continue
        date_str = date.strftime("%Y-%m-%d")

        ma200 = row.get("MA200")
        if pd.notna(ma200) and close > ma200:
            signals.append({
                "ticker":          ticker,
                "signal_type":     "price_gt_ma200",
                "signal_date":     date_str,
                "close_price":     round(float(close), 4),
                "indicator_value": round(float(ma200), 4),
            })

        ma10 = row.get("MA10")
        ma30 = row.get("MA30")
        if pd.notna(ma10) and pd.notna(ma30) and ma10 > ma30:
            signals.append({
                "ticker":          ticker,
                "signal_type":     "ma10_gt_ma30",
                "signal_date":     date_str,
                "close_price":     round(float(close), 4),
                "indicator_value": round(float(ma10), 4),
            })

    return signals


# ---------------------------------------------------------------------------
# Database writes
# ---------------------------------------------------------------------------

def _upsert_daily_rows(conn, ticker: str, data: pd.DataFrame, fetched_at: str):
    rows = []
    for date, row in data.iterrows():
        rows.append((
            ticker,
            date.strftime("%Y-%m-%d"),
            _safe(row.get("Open")),
            _safe(row.get("High")),
            _safe(row.get("Low")),
            _safe(row.get("Close")),
            _safe_int(row.get("Volume")),
            _safe(row.get("MA6")),
            _safe(row.get("MA10")),
            _safe(row.get("MA30")),
            _safe(row.get("MA50")),
            _safe(row.get("MA200")),
            _safe(row.get("High_30D")),
            _safe(row.get("Low_30D")),
            _safe(row.get("Vol_MA10")),
            _safe(row.get("Price_Change")),
            _safe(row.get("Pct_Change")),
            row.get("Direction", ""),
            _safe(row.get("RSI14")),
            _safe(row.get("MA100")),
            _safe(row.get("MA150")),
            _safe(row.get("High_52wk")),
            _safe(row.get("Low_52wk")),
            _safe(row.get("Low_AllTime")),
            _safe_date(row.get("Low_AllTime_Date")),
            _safe(row.get("RS_raw")),
            _safe_int(row.get("C1")),
            _safe_int(row.get("C2")),
            _safe_int(row.get("C3")),
            _safe_int(row.get("C4")),
            _safe_int(row.get("C5")),
            _safe_int(row.get("C6")),
            _safe_int(row.get("C8")),
            fetched_at,
        ))

    conn.executemany("""
        INSERT INTO stocks_daily
            (ticker, date, open, high, low, close, volume,
             ma6, ma10, ma30, ma50, ma200,
             high_30d, low_30d, vol_ma10,
             price_change, pct_change, direction, rsi14,
             ma100, ma150, high_52wk, low_52wk, low_alltime, low_alltime_date, rs_raw,
             c1, c2, c3, c4, c5, c6, c8,
             fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker, date) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume,
            ma6=excluded.ma6, ma10=excluded.ma10, ma30=excluded.ma30,
            ma50=excluded.ma50, ma200=excluded.ma200,
            high_30d=excluded.high_30d, low_30d=excluded.low_30d,
            vol_ma10=excluded.vol_ma10,
            price_change=excluded.price_change, pct_change=excluded.pct_change,
            direction=excluded.direction, rsi14=excluded.rsi14,
            ma100=excluded.ma100, ma150=excluded.ma150, high_52wk=excluded.high_52wk,
            low_52wk=excluded.low_52wk, low_alltime=excluded.low_alltime,
            low_alltime_date=excluded.low_alltime_date, rs_raw=excluded.rs_raw,
            c1=excluded.c1, c2=excluded.c2, c3=excluded.c3, c4=excluded.c4,
            c5=excluded.c5, c6=excluded.c6, c8=excluded.c8,
            rs_rank=NULL, c7=NULL, trend_score=NULL,
            fetched_at=excluded.fetched_at
    """, rows)


def _splice_stored_history(conn, ticker: str, data: pd.DataFrame):
    """Prepend the OHLCV already stored in stocks_daily (rows older than this download) to the
    fresh download, so the rolling indicators (MA150/MA200, 52-week range, RS 252-day, ...) are
    computed on ALL the history we hold, not just the 2y the download happens to cover.
    Without this, every Fetch left MA200 blank for the first 200 days of its window and quietly
    broke older signals / backtests. Returns (combined, first_new_date); only rows from
    first_new_date onward should be written back."""
    fresh = data[["Open", "High", "Low", "Close", "Volume"]].copy()
    fresh.index = pd.to_datetime(fresh.index)
    if fresh.index.tz is not None:
        fresh.index = fresh.index.tz_localize(None)
    fresh.index = fresh.index.normalize()
    first_new = fresh.index.min()
    hist = pd.read_sql_query(
        "SELECT date, open AS Open, high AS High, low AS Low, close AS Close, volume AS Volume "
        "FROM stocks_daily WHERE ticker = ? AND date < ? AND close IS NOT NULL ORDER BY date",
        conn, params=(ticker, first_new.strftime("%Y-%m-%d")))
    if hist.empty:
        return fresh, first_new
    hist.index = pd.to_datetime(hist.pop("date"))
    return pd.concat([hist, fresh]), first_new


def _log_skipped(conn, ticker: str, reason: str):
    """One row per ticker: repeated skips/errors update the existing row
    instead of accumulating a new one on every fetch attempt."""
    conn.execute("""
        INSERT INTO skipped_stocks (ticker, reason, skipped_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(ticker) DO UPDATE SET
            reason = excluded.reason,
            skipped_at = excluded.skipped_at
    """, (ticker, reason))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return round(float(val), 6)
    except (TypeError, ValueError):
        return None


def _safe_int(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _safe_date(val):
    if val is None or pd.isna(val):
        return None
    try:
        return pd.Timestamp(val).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Cross-ticker RS Rank + Trend Template post-pass
# ---------------------------------------------------------------------------

def _compute_rs_rank(conn, emit, table="stocks_daily", since=None):
    """Cross-sectional percentile rank (1-99) of RS_raw per trading date.
    `since` (YYYY-MM-DD) limits it to those dates — exact, since each date is ranked on its own."""
    emit(f"RS Rank ({table}): loading RS_raw data...")
    raw = conn.execute(
        f"SELECT ticker, date, rs_raw FROM {table} WHERE rs_raw IS NOT NULL"
        + (" AND date >= ?" if since else "") + " ORDER BY date",
        (since,) if since else (),
    ).fetchall()

    by_date = {}
    for r in raw:
        by_date.setdefault(r["date"], []).append((r["ticker"], r["rs_raw"]))

    updates = []
    for date, entries in by_date.items():
        n = len(entries)
        if n < 2:
            continue
        sorted_e = sorted(entries, key=lambda x: x[1], reverse=True)
        for rank_0, (ticker, _) in enumerate(sorted_e):
            pct = 1.0 - rank_0 / (n - 1)          # 1.0 = best, 0.0 = worst
            rs_rank = max(1, min(99, math.ceil(pct * 99)))
            updates.append((rs_rank, ticker, date))

    if updates:
        conn.executemany(
            f"UPDATE {table} SET rs_rank = ? WHERE ticker = ? AND date = ?",
            updates,
        )
    emit(f"RS Rank ({table}): updated {len(updates)} rows across {len(by_date)} dates.")


def _compute_trend_template(conn, emit, table="stocks_daily", since=None):
    """Compute C7 and trend_score for every row via SQL.

    C7 baseline is the self-relative rs_rank (>=70) among currently-tracked
    tickers, same as before. Wherever a real market-wide RS Rating exists in
    rs_ratings (S&P 500 + Russell 1000 universe — see universe.py /
    rs_calculator.py) for that exact ticker+date, it overrides the baseline
    so C7 reflects the real universe percentile instead of the self-relative
    approximation.

    `table` is always an internal literal ("stocks_daily" or
    "universe_prices"), never user input, so f-string interpolation is safe.
    """
    w_where = " WHERE date >= ?" if since else ""          # for the statements without a WHERE
    w_and = f" AND {table}.date >= ?" if since else ""     # for the one that already has a WHERE
    args = (since,) if since else ()
    conn.execute(f"""
        UPDATE {table} SET
            c7 = CASE
                    WHEN rs_rank IS NULL  THEN NULL
                    WHEN rs_rank >= 70    THEN 1
                    ELSE 0
                 END
        {w_where}
    """, args)
    conn.execute(f"""
        UPDATE {table}
        SET c7 = (
            SELECT CASE WHEN r.rs_rating >= 70 THEN 1 ELSE 0 END
            FROM rs_ratings r
            WHERE r.ticker = {table}.ticker AND r.date = {table}.date
        )
        WHERE EXISTS (
            SELECT 1 FROM rs_ratings r
            WHERE r.ticker = {table}.ticker AND r.date = {table}.date
        ){w_and}
    """, args)
    conn.execute(f"""
        UPDATE {table} SET
            trend_score = (
                COALESCE(c1,0) + COALESCE(c2,0) + COALESCE(c3,0) +
                COALESCE(c4,0) + COALESCE(c5,0) + COALESCE(c6,0) +
                COALESCE(c7,0) + COALESCE(c8,0)
            )
        {w_where}
    """, args)
    emit(f"Trend Template ({table}): C7 and scores updated.")


def _compute_universe_indicators(conn, tickers: list[str], emit, since=None):
    """Compute MAs / 52wk hi-lo / Trend Template C1-C6,C8 for every universe
    ticker's stored OHLCV history in universe_prices, mirroring what
    _upsert_daily_rows does for stocks_daily. Reuses _calculate_indicators()
    since it's already ticker-agnostic — only the read/write side (SQLite
    table instead of a yfinance download) differs from the tracked-ticker
    path."""
    emit(f"Universe indicators: computing for {len(tickers)} tickers...")
    updates = []
    processed = 0
    for ticker in tickers:
        df = pd.read_sql_query(
            "SELECT date, open, high, low, close, volume FROM universe_prices "
            "WHERE ticker = ? ORDER BY date",
            conn, params=(ticker,),
        )
        if df.empty:
            continue
        df = df.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume",
        })
        df.index = pd.to_datetime(df.pop("date"))
        calc = _calculate_indicators(df)          # always on the FULL stored history ...
        if since:                                  # ... but only the recent rows are written back
            calc = calc.loc[calc.index >= pd.Timestamp(since)]
        for date_, row in calc.iterrows():
            updates.append((
                _safe(row.get("MA6")), _safe(row.get("MA10")), _safe(row.get("MA30")),
                _safe(row.get("MA50")), _safe(row.get("MA150")), _safe(row.get("MA200")),
                _safe(row.get("High_30D")), _safe(row.get("Low_30D")),
                _safe(row.get("High_52wk")), _safe(row.get("Low_52wk")), _safe(row.get("Low_AllTime")),
                _safe_date(row.get("Low_AllTime_Date")),
                _safe(row.get("Vol_MA10")),
                _safe(row.get("Price_Change")), _safe(row.get("Pct_Change")),
                row.get("Direction", ""),
                _safe(row.get("RSI14")), _safe(row.get("RS_raw")),
                _safe_int(row.get("C1")), _safe_int(row.get("C2")), _safe_int(row.get("C3")),
                _safe_int(row.get("C4")), _safe_int(row.get("C5")), _safe_int(row.get("C6")),
                _safe_int(row.get("C8")),
                ticker, date_.strftime("%Y-%m-%d"),
            ))
        processed += 1

    if updates:
        conn.executemany("""
            UPDATE universe_prices SET
                ma6=?, ma10=?, ma30=?, ma50=?, ma150=?, ma200=?,
                high_30d=?, low_30d=?, high_52wk=?, low_52wk=?, low_alltime=?, low_alltime_date=?, vol_ma10=?,
                price_change=?, pct_change=?, direction=?, rsi14=?, rs_raw=?,
                c1=?, c2=?, c3=?, c4=?, c5=?, c6=?, c8=?,
                rs_rank=NULL, c7=NULL, trend_score=NULL
            WHERE ticker=? AND date=?
        """, updates)
    emit(f"Universe indicators: updated {len(updates)} rows across {processed} tickers.")


def _update_rs_universe(conn, emit):
    """Part 5: weekly universe refresh (if due) + batched universe price
    fetch + indicator/Trend-Template computation + RS Score/Rating/Line calc
    for today, run after the tracked tickers' own price data has been
    ingested."""
    if universe.needs_refresh(conn):
        universe.refresh_universe(conn, progress_cb=emit)
        conn.commit()

    active = universe.get_active_universe(conn)
    if not active:
        emit("RS Universe: no active universe — skipping RS calc")
        return
    if len(active) < universe.RS_UNIVERSE_SIZE_WARNING:
        emit(
            f"WARNING: universe has only {len(active)} tickers — RS Ratings will be "
            f"relative to tracked universe only, not full market"
        )
    # fetch_universe_prices() below opens its own nested connections for the
    # batched OHLCV download — commit first so they don't deadlock against
    # any writes still pending on this connection.
    conn.commit()

    rs_calculator.fetch_universe_prices(active, progress_cb=emit)
    _compute_universe_indicators(conn, active, emit)

    today = datetime.now().strftime("%Y-%m-%d")
    summary = rs_calculator.compute_rs_ratings(conn, dates=[today], progress_cb=emit)
    emit(f"RS Ratings: {summary}")

    _compute_rs_rank(conn, emit, table="universe_prices")
    _compute_trend_template(conn, emit, table="universe_prices")


# ---------------------------------------------------------------------------
# Core fetch loop
# ---------------------------------------------------------------------------

def fetch_all(tickers: list[str], period: str = "2y", progress_cb=None, skip_universe: bool = False):
    def _emit(msg: str):
        if progress_cb:
            progress_cb(msg)
        else:
            print(msg, flush=True)

    setup_database()
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = len(tickers)

    with get_connection() as conn:
        for i, ticker in enumerate(tickers, 1):
            try:
                stock = yf.Ticker(ticker)
                data = stock.history(period=period)

                if data.empty:
                    _emit(f"SKIP {ticker} — no data returned by yfinance")
                    _log_skipped(conn, ticker, "yfinance returned empty data")
                    continue

                data, first_new = _splice_stored_history(conn, ticker, data)
                data = _calculate_indicators(data)
                _upsert_daily_rows(conn, ticker, data.loc[data.index >= first_new], fetched_at)

                # Summarise only the *unique* signal types active on the latest row
                latest_signals = _detect_signals(ticker, data.iloc[[-1]])
                signal_labels  = list({s["signal_type"] for s in latest_signals})
                _emit(f"OK {ticker} — {len(data)} rows" + (f" | signals: {signal_labels}" if signal_labels else ""))

            except Exception as exc:
                _emit(f"ERROR {ticker} — {exc}")
                _log_skipped(conn, ticker, str(exc))

    _emit(f"Done fetching — {total} tickers processed.")

    with get_connection() as conn:
        _compute_rs_rank(conn, _emit)
        conn.commit()
        if skip_universe:
            _emit("Universe refresh skipped for this fetch (per-run toggle).")
        else:
            # _update_rs_universe opens its own nested connections (batched
            # universe price fetch) — the commit above prevents those from
            # deadlocking against this connection's still-open transaction.
            _update_rs_universe(conn, _emit)
            conn.commit()
        _compute_trend_template(conn, _emit)

    _emit(f"All done. DB: {DB_PATH}")


# ---------------------------------------------------------------------------
# Ticker resolution
# ---------------------------------------------------------------------------

def quick_update(progress_cb=None, days: int = 20, chunk: int = 200) -> dict:
    """FAST US update for the Surge Strategy (and everything else that reads recent prices).

    Downloads only the last `days` calendar days, in batches of `chunk` tickers, for every tracked
    ticker + the active S&P 500 / Russell 1000 universe + the RS benchmark; splices the new days
    onto the stored history (so MA200/52wk/RS keep their full history), recomputes the indicators
    for the recent rows only, then refreshes RS Rating, RS Rank and the Trend Template for those
    dates. Same result as the full Fetch for the recent dates, without the per-ticker requests and
    without rewriting two years of rows. Tracked tickers with no stored history are skipped (they
    need one full Fetch first). Returns a summary dict."""
    def _emit(msg: str):
        (progress_cb or (lambda m: print(m, flush=True)))(msg)

    setup_database()
    t0 = time.time()
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    since = (date.today() - timedelta(days=days)).isoformat()
    bench = rs_calculator.RS_BENCHMARK

    with get_connection() as conn:
        tracked = [r["ticker"] for r in conn.execute("SELECT ticker FROM extraction_tickers ORDER BY ticker")]
        active = universe.get_active_universe(conn)
        have_hist = {r["ticker"] for r in conn.execute("SELECT DISTINCT ticker FROM stocks_daily")}
        prev_uni_max = conn.execute("SELECT MAX(date) FROM universe_prices").fetchone()[0]
        prev_max = conn.execute("SELECT MAX(date) FROM stocks_daily").fetchone()[0]
    uni_write = set(active) | {bench}
    tracked_set = set(tracked)
    symbols = list(dict.fromkeys(tracked + active + [bench]))
    _emit(f"Quick US update: {len(symbols)} tickers ({len(tracked)} tracked + {len(active)} universe), "
          f"last {days} days (from {since}); stored data currently runs through {prev_max}")

    stats = {"tickers": len(symbols), "tracked_updated": 0, "no_history": [], "universe_rows": 0}
    frames = {}
    with get_connection() as conn:
        for i in range(0, len(symbols), chunk):
            part = symbols[i:i + chunk]
            try:
                df = yf.download(part, start=since, interval="1d", group_by="ticker",
                                 auto_adjust=True, progress=False, threads=True)
            except Exception as exc:
                _emit(f"  batch {i // chunk + 1} failed: {exc}")
                continue
            stats["universe_rows"] += rs_calculator._upsert_prices_from_download(
                conn, df, [t for t in part if t in uni_write], fetched_at)
            multi = isinstance(df.columns, pd.MultiIndex)
            for t in part:
                if t not in tracked_set:
                    continue
                try:
                    sub = (df[t] if multi else df).dropna(subset=["Close"])[["Open", "High", "Low", "Close", "Volume"]]
                except Exception:
                    continue
                if not sub.empty:
                    frames[t] = sub
            conn.commit()
            _emit(f"  downloaded {min(i + chunk, len(symbols))}/{len(symbols)} tickers ({time.time() - t0:.0f}s)")

        _emit(f"Tracked tickers: recomputing indicators for {len(frames)} tickers...")
        for t, sub in frames.items():
            if t not in have_hist:
                stats["no_history"].append(t)
                continue
            combined, first_new = _splice_stored_history(conn, t, sub)
            calc = _calculate_indicators(combined)
            _upsert_daily_rows(conn, t, calc.loc[calc.index >= first_new], fetched_at)
            stats["tracked_updated"] += 1
        conn.commit()
        if stats["no_history"]:
            _emit(f"  skipped {len(stats['no_history'])} tracked ticker(s) with no stored history "
                  f"(run the full Fetch once): {', '.join(stats['no_history'][:10])}")

        _compute_universe_indicators(conn, active, _emit, since=since)
        conn.commit()

        new_dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM universe_prices WHERE date >= ? ORDER BY date", (prev_uni_max or since,))]
        if new_dates:
            _emit(f"RS Rating for {len(new_dates)} date(s): {new_dates[0]} .. {new_dates[-1]}")
            rs_calculator.compute_rs_ratings(conn, dates=new_dates, progress_cb=_emit)
            conn.commit()
        _compute_rs_rank(conn, _emit, table="stocks_daily", since=since)
        _compute_rs_rank(conn, _emit, table="universe_prices", since=since)
        conn.commit()
        _compute_trend_template(conn, _emit, table="universe_prices", since=since)
        _compute_trend_template(conn, _emit, table="stocks_daily", since=since)
        conn.commit()
        stats["data_through"] = conn.execute("SELECT MAX(date) FROM stocks_daily").fetchone()[0]
        stats["universe_through"] = conn.execute("SELECT MAX(date) FROM universe_prices").fetchone()[0]

    stats["seconds"] = round(time.time() - t0, 1)
    _emit(f"Quick US update done in {stats['seconds']}s — tracked data through {stats['data_through']}, "
          f"universe through {stats['universe_through']}.")
    return stats


def recalculate_indicators(tickers=None, progress_cb=None, commit_every: int = 25):
    """Recompute every indicator for the tracked tickers from the OHLCV ALREADY STORED in
    stocks_daily (no network), then refresh RS Rank and the Trend Template. Use it to repair
    rows whose MA200 / 52-week / RS values were computed on a short download window."""
    def _emit(msg: str):
        (progress_cb or (lambda m: print(m, flush=True)))(msg)

    setup_database()
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_connection() as conn:
        if not tickers:
            tickers = [r["ticker"] for r in conn.execute("SELECT DISTINCT ticker FROM stocks_daily ORDER BY ticker")]
        for i, ticker in enumerate(tickers, 1):
            df = pd.read_sql_query(
                "SELECT date, open AS Open, high AS High, low AS Low, close AS Close, volume AS Volume "
                "FROM stocks_daily WHERE ticker = ? AND close IS NOT NULL ORDER BY date", conn, params=(ticker,))
            if df.empty:
                continue
            df.index = pd.to_datetime(df.pop("date"))
            _upsert_daily_rows(conn, ticker, _calculate_indicators(df), fetched_at)
            if i % commit_every == 0:
                conn.commit()
                _emit(f"  {i}/{len(tickers)} tickers recalculated")
        conn.commit()
        _emit(f"Recalculated indicators for {len(tickers)} tickers from stored history.")
        _compute_rs_rank(conn, _emit)
        conn.commit()
        _compute_trend_template(conn, _emit)
        conn.commit()


def get_tickers_from_db() -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT ticker FROM extraction_tickers ORDER BY ticker"
        ).fetchall()
    return [r["ticker"] for r in rows]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch stock data into SQLite")
    parser.add_argument("--tickers", nargs="+", metavar="TICKER",
                        help="Override ticker list (space-separated)")
    parser.add_argument("--period", default="2y",
                        help="yfinance history period (default: 2y)")
    parser.add_argument("--skip-universe", action="store_true",
                        help="Skip the S&P500+Russell1000 universe refresh for this run")
    parser.add_argument("--quick", action="store_true",
                        help="Fast update: last 20 days for all tracked + universe tickers (batched), then RS refresh")
    parser.add_argument("--recalculate", action="store_true",
                        help="No download: recompute all indicators for the tracked tickers from the stored history")
    args = parser.parse_args()

    if args.quick:
        quick_update()
        sys.exit(0)

    if args.recalculate:
        recalculate_indicators([t.upper() for t in args.tickers] if args.tickers else None)
        sys.exit(0)

    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
    else:
        tickers = get_tickers_from_db()
        if not tickers:
            print("No tickers found in extraction_tickers table.")
            print("Add tickers via the dashboard, or pass --tickers AAPL MSFT ...")
            sys.exit(0)

    print(f"Tickers to fetch: {tickers}\n")
    fetch_all(tickers, period=args.period, skip_universe=args.skip_universe)
