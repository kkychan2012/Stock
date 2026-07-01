"""
Headless daily fetcher.

Run:  python fetch_data.py
      python fetch_data.py --tickers AAPL MSFT   # override ticker list
      python fetch_data.py --period 1y            # default is 2y
"""

import argparse
import math
import sys
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from db_setup import get_connection, setup_database, DB_PATH


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

    Scans ALL rows so the breakout_signals table is fully populated when the
    fetcher runs, not just the single latest date.  The UNIQUE constraint on
    (ticker, signal_type, signal_date) in the DB prevents duplicates.
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
            _safe(row.get("MA150")),
            _safe(row.get("High_52wk")),
            _safe(row.get("Low_52wk")),
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
             ma150, high_52wk, low_52wk, rs_raw,
             c1, c2, c3, c4, c5, c6, c8,
             fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker, date) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume,
            ma6=excluded.ma6, ma10=excluded.ma10, ma30=excluded.ma30,
            ma50=excluded.ma50, ma200=excluded.ma200,
            high_30d=excluded.high_30d, low_30d=excluded.low_30d,
            vol_ma10=excluded.vol_ma10,
            price_change=excluded.price_change, pct_change=excluded.pct_change,
            direction=excluded.direction, rsi14=excluded.rsi14,
            ma150=excluded.ma150, high_52wk=excluded.high_52wk,
            low_52wk=excluded.low_52wk, rs_raw=excluded.rs_raw,
            c1=excluded.c1, c2=excluded.c2, c3=excluded.c3, c4=excluded.c4,
            c5=excluded.c5, c6=excluded.c6, c8=excluded.c8,
            rs_rank=NULL, c7=NULL, trend_score=NULL,
            fetched_at=excluded.fetched_at
    """, rows)


def _upsert_signals(conn, signals: list[dict]):
    for s in signals:
        conn.execute("""
            INSERT INTO breakout_signals
                (ticker, signal_type, signal_date, close_price, indicator_value)
            VALUES (:ticker, :signal_type, :signal_date, :close_price, :indicator_value)
            ON CONFLICT(ticker, signal_type, signal_date) DO NOTHING
        """, s)


def _log_skipped(conn, ticker: str, reason: str):
    conn.execute(
        "INSERT INTO skipped_stocks (ticker, reason) VALUES (?, ?)",
        (ticker, reason)
    )


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


# ---------------------------------------------------------------------------
# Cross-ticker RS Rank + Trend Template post-pass
# ---------------------------------------------------------------------------

def _compute_rs_rank(conn, emit):
    """Cross-sectional percentile rank (1-99) of RS_raw per trading date."""
    emit("RS Rank: loading RS_raw data...")
    raw = conn.execute(
        "SELECT ticker, date, rs_raw FROM stocks_daily WHERE rs_raw IS NOT NULL ORDER BY date"
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
            "UPDATE stocks_daily SET rs_rank = ? WHERE ticker = ? AND date = ?",
            updates,
        )
    emit(f"RS Rank: updated {len(updates)} rows across {len(by_date)} dates.")


def _compute_trend_template(conn, emit):
    """Compute C7 (rs_rank >= 70) and trend_score for every row via SQL."""
    conn.execute("""
        UPDATE stocks_daily SET
            c7 = CASE
                    WHEN rs_rank IS NULL  THEN NULL
                    WHEN rs_rank >= 70    THEN 1
                    ELSE 0
                 END,
            trend_score = (
                COALESCE(c1,0) + COALESCE(c2,0) + COALESCE(c3,0) +
                COALESCE(c4,0) + COALESCE(c5,0) + COALESCE(c6,0) +
                CASE WHEN rs_rank IS NOT NULL AND rs_rank >= 70 THEN 1 ELSE 0 END +
                COALESCE(c8,0)
            )
    """)
    emit("Trend Template: C7 and scores updated.")


# ---------------------------------------------------------------------------
# Core fetch loop
# ---------------------------------------------------------------------------

def fetch_all(tickers: list[str], period: str = "2y", progress_cb=None):
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

                data = _calculate_indicators(data)
                _upsert_daily_rows(conn, ticker, data, fetched_at)

                signals = _detect_signals(ticker, data)
                _upsert_signals(conn, signals)

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
        _compute_trend_template(conn, _emit)

    _emit(f"All done. DB: {DB_PATH}")


# ---------------------------------------------------------------------------
# Ticker resolution
# ---------------------------------------------------------------------------

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
    args = parser.parse_args()

    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
    else:
        tickers = get_tickers_from_db()
        if not tickers:
            print("No tickers found in extraction_tickers table.")
            print("Add tickers via the dashboard, or pass --tickers AAPL MSFT ...")
            sys.exit(0)

    print(f"Tickers to fetch: {tickers}\n")
    fetch_all(tickers, period=args.period)
