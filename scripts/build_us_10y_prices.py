"""
Build a long-history US price database for the Volume Surge backtest.

The dashboard's own stock_dashboard.db only reaches back to 2024, so a 10-year
backtest needs its own data. This downloads daily bars from Yahoo (adjusted, the
same auto_adjust=True the dashboard Fetch uses) for the US universe -- tracked
Ticker List + active S&P 500 / Russell 1000, read from stock_dashboard.db --
and writes them, with the three indicators the surge study needs (Vol MA10,
MA50, MA200 -- same formulas as fetch_data._calculate_indicators), into a
SEPARATE database. stock_dashboard.db is only read, never written.

The output has the table names scripts/study_volume_surge_2026_04.py's
load_series_by_ticker() expects (everything lands in stocks_daily), so
    python scripts/us_backtest_report.py --db us_backtest_10y/prices_10y.db ...
runs the unchanged study code on it.

Known bias: the ticker list is TODAY's index membership -- companies that were
acquired, went bankrupt or dropped out of the indexes during the period are
missing (survivorship), which flatters any long backtest.

Run from the project root:
    python scripts/build_us_10y_prices.py                 # from 2015-09-01
    python scripts/build_us_10y_prices.py --start 2012-01-01
"""
import argparse
import os
import sqlite3
import time

import pandas as pd
import yfinance as yf

SRC_DB = "stock_dashboard.db"
OUT_DB = os.path.join("us_backtest_10y", "prices_10y.db")
CHUNK = 100


def us_tickers():
    conn = sqlite3.connect(SRC_DB)
    tickers = {r[0] for r in conn.execute("SELECT ticker FROM ticker_universe WHERE is_active = 1")}
    tickers |= {r[0] for r in conn.execute("SELECT ticker FROM extraction_tickers")}
    conn.close()
    # US only: drop foreign listings (2800.HK ...), indexes (^GSPC) and futures/FX (=)
    return sorted(t for t in tickers if "." not in t and not t.startswith("^") and "=" not in t)


def main():
    ap = argparse.ArgumentParser(description="download long-history US daily bars for the surge backtest")
    ap.add_argument("--start", default="2015-09-01",
                    help="first date to download (MA200 needs ~10 months of warm-up before the first surge month)")
    ap.add_argument("--out", default=OUT_DB)
    args = ap.parse_args()

    tickers = us_tickers()
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    if os.path.exists(args.out):
        os.remove(args.out)
    out = sqlite3.connect(args.out)
    cols = "ticker TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, vol_ma10 REAL, ma50 REAL, ma200 REAL"
    out.execute(f"CREATE TABLE stocks_daily ({cols}, PRIMARY KEY (ticker, date))")
    out.execute(f"CREATE TABLE universe_prices ({cols})")          # empty: only so the study's loader query runs
    out.execute("CREATE TABLE ticker_universe (ticker TEXT, is_active INTEGER)")

    print(f"{len(tickers)} US tickers from {args.start} -> {args.out}")
    t0, got, missing, rows_total = time.time(), 0, [], 0
    for n in range(0, len(tickers), CHUNK):
        part = tickers[n:n + CHUNK]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(part, start=args.start, interval="1d", group_by="ticker",
                                 auto_adjust=True, progress=False, threads=True)
                break
            except Exception as e:                                # one bad batch must not abort the run
                print(f"  batch {n // CHUNK + 1} attempt {attempt + 1} failed: {e}")
                time.sleep(5)
        for t in part:
            try:
                d = df[t].dropna(subset=["Open", "Close"]) if df is not None else None
            except KeyError:
                d = None
            if d is None or d.empty:
                missing.append(t)
                continue
            d = d.copy()
            d["vol_ma10"] = d["Volume"].rolling(window=10).mean()
            d["ma50"] = d["Close"].rolling(window=50).mean()
            d["ma200"] = d["Close"].rolling(window=200).mean()
            d = d.astype(object).where(d.notna(), None)
            out.executemany(
                "INSERT INTO stocks_daily VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(t, idx.strftime("%Y-%m-%d"), r.Open, r.High, r.Low, r.Close, r.Volume, r.vol_ma10, r.ma50, r.ma200)
                 for idx, r in zip(d.index, d.itertuples(index=False))])
            got += 1
            rows_total += len(d)
        out.commit()
        print(f"  {min(n + CHUNK, len(tickers))}/{len(tickers)} tickers, {rows_total:,} rows, {time.time() - t0:.0f}s", flush=True)

    lo, hi = out.execute("SELECT MIN(date), MAX(date) FROM stocks_daily").fetchone()
    out.close()
    print(f"done: {got} tickers, {rows_total:,} rows, {lo} .. {hi}, {time.time() - t0:.0f}s")
    if missing:
        print(f"no data for {len(missing)}: {' '.join(missing)}")


if __name__ == "__main__":
    main()
