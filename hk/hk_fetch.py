"""
Hong Kong price fetch for the volume-surge simulation — STANDALONE.

Writes only to hk/hk_stocks.db (tables hk_daily, hk_securities). It never opens or
modifies stock_dashboard.db and imports nothing from the dashboard (no api_server,
no db_setup, no fetch_data), so it cannot affect the running dashboard.

Two ticker universes:
  * Hang Seng Index (default): hk/hk_tickers.txt, one Yahoo ticker per line
    ("0700.HK  # Tencent"), created from Wikipedia on first run. Editable.
  * Every HKEX-listed equity (--all): hk/hk_tickers_all.txt, built from HKEX's
    official "List of Securities" — category Equity + REITs, HKD counters only
    (RMB/USD duplicate counters, warrants, CBBCs, bonds and ETFs are excluded).

Run from the project root:
    python hk/hk_fetch.py                    # Hang Seng Index names
    python hk/hk_fetch.py --all              # every listed equity (~2,800 tickers, a few minutes)
    python hk/hk_fetch.py --refresh-list     # re-scrape the HSI list
    python hk/hk_fetch.py --start 2022-06-01
"""
import argparse
import io
import os
import re
import sqlite3
import time
import urllib.request

import pandas as pd
import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "hk_stocks.db")
HSI_FILE = os.path.join(HERE, "hk_tickers.txt")
ALL_FILE = os.path.join(HERE, "hk_tickers_all.txt")
DEFAULT_START = "2023-01-01"   # ~1 year before the first study month so MA200 is valid
HKEX_LIST_URL = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def scrape_hsi():
    """[(yahoo_ticker, name)] for the current Hang Seng Index constituents."""
    html = _get("https://en.wikipedia.org/wiki/Hang_Seng_Index", 30).decode("utf-8", "replace")
    for t in pd.read_html(io.StringIO(html)):
        if "Ticker" in t.columns and len(t) > 40:
            out = []
            for tk, name in zip(t["Ticker"], t.iloc[:, 1]):
                digits = re.sub(r"\D", "", str(tk))
                if digits:
                    out.append((digits.zfill(4) + ".HK", str(name).strip()))
            return out
    raise RuntimeError("Hang Seng Index constituents table not found on Wikipedia")


def scrape_hkex_equities():
    """Every HKD-traded equity / REIT on HKEX (main board + GEM) from the official list.
    Returns [dict(ticker, name, category, sub_category, board_lot)]."""
    df = pd.read_excel(io.BytesIO(_get(HKEX_LIST_URL, 120)), header=2, dtype=str)
    df.columns = [c.split("\n")[0].strip() for c in df.columns]
    df = df[df["Category"].isin(["Equity", "Real Estate Investment Trusts"])]
    df = df[df["Trading Currency"].fillna("HKD") == "HKD"]
    out = []
    for _, r in df.iterrows():
        code = re.sub(r"\D", "", str(r["Stock Code"]))
        if not code or int(code) >= 10000:            # 5-digit codes = RMB counters / preference lines
            continue
        out.append({"ticker": f"{int(code):04d}.HK", "name": str(r["Name of Securities"]).strip(),
                    "category": r["Category"], "sub_category": r.get("Sub-Category"),
                    "board_lot": int(str(r["Board Lot"]).replace(",", "")) if pd.notna(r.get("Board Lot")) else None})
    return list({o["ticker"]: o for o in out}.values())


def write_ticker_file(path, pairs, header):
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n")
        for tk, name in pairs:
            f.write(f"{tk}  # {name}\n")


def read_ticker_file(path):
    tickers = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            tk = line.split("#", 1)[0].strip().upper()
            if tk:
                tickers.append(tk)
    return list(dict.fromkeys(tickers))


def add_indicators(df):
    """MA50 / MA200 of close and the 10-day average volume — same definitions as
    the main pipeline's _calculate_indicators()."""
    df = df.copy()
    df["ma50"] = df["Close"].rolling(50).mean()
    df["ma200"] = df["Close"].rolling(200).mean()
    df["vol_ma10"] = df["Volume"].rolling(10).mean()
    return df


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hk_daily (
            ticker   TEXT NOT NULL,
            date     TEXT NOT NULL,
            open     REAL, high REAL, low REAL, close REAL,
            volume   INTEGER,
            vol_ma10 REAL, ma50 REAL, ma200 REAL,
            PRIMARY KEY (ticker, date)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hk_securities (
            ticker TEXT PRIMARY KEY, name TEXT, category TEXT, sub_category TEXT, board_lot INTEGER
        )""")


def _num(v):
    return None if pd.isna(v) else float(v)


def fetch(conn, tickers, start, chunk):
    ok, empty = 0, []
    t0 = time.time()
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        try:
            df = yf.download(part, start=start, interval="1d", group_by="ticker",
                             auto_adjust=True, progress=False, threads=True)
        except Exception as exc:
            print(f"  chunk {i // chunk + 1} failed: {exc}")
            empty += part
            continue
        multi = isinstance(df.columns, pd.MultiIndex)
        for t in part:
            try:
                sub = (df[t] if multi else df).dropna(subset=["Close"])
            except Exception:
                sub = pd.DataFrame()
            if sub.empty:
                empty.append(t)
                continue
            sub = add_indicators(sub)
            rows = [(t, idx.strftime("%Y-%m-%d"), _num(r["Open"]), _num(r["High"]), _num(r["Low"]),
                     _num(r["Close"]), None if pd.isna(r["Volume"]) else int(r["Volume"]),
                     _num(r["vol_ma10"]), _num(r["ma50"]), _num(r["ma200"]))
                    for idx, r in sub.iterrows()]
            conn.executemany("INSERT OR REPLACE INTO hk_daily VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            ok += 1
        conn.commit()
        done = min(i + chunk, len(tickers))
        print(f"  {done}/{len(tickers)} tickers processed ({time.time() - t0:.0f}s, {ok} with data)", flush=True)
    return ok, empty


def main():
    ap = argparse.ArgumentParser(description="Fetch Hong Kong daily prices into hk/hk_stocks.db")
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--all", action="store_true", help="every HKEX-listed equity instead of the Hang Seng Index")
    ap.add_argument("--refresh-list", action="store_true", help="re-scrape the ticker list first")
    ap.add_argument("--chunk", type=int, default=60, help="tickers per Yahoo download request")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.all:
        if args.refresh_list or not os.path.exists(ALL_FILE):
            secs = scrape_hkex_equities()
            write_ticker_file(ALL_FILE, [(s["ticker"], s["name"]) for s in secs],
                              "# Every HKEX-listed equity + REIT (HKD counters), from HKEX's List of Securities.")
            conn.executemany("INSERT OR REPLACE INTO hk_securities VALUES (?,?,?,?,?)",
                             [(s["ticker"], s["name"], s["category"], s["sub_category"], s["board_lot"]) for s in secs])
            conn.commit()
            print(f"Wrote {len(secs)} HKEX-listed tickers to {ALL_FILE}")
        tickers = read_ticker_file(ALL_FILE)
    else:
        if args.refresh_list or not os.path.exists(HSI_FILE):
            pairs = scrape_hsi()
            write_ticker_file(HSI_FILE, pairs,
                              "# Hang Seng Index constituents (Yahoo format). Edit freely; "
                              "re-scrape with: python hk/hk_fetch.py --refresh-list")
            print(f"Wrote {len(pairs)} Hang Seng Index tickers to {HSI_FILE}")
        tickers = read_ticker_file(HSI_FILE)

    print(f"Fetching {len(tickers)} tickers from {args.start} ...", flush=True)
    ok, empty = fetch(conn, tickers, args.start, args.chunk)

    n, lo, hi, nt = conn.execute(
        "SELECT COUNT(*), MIN(date), MAX(date), COUNT(DISTINCT ticker) FROM hk_daily").fetchone()
    conn.close()
    print(f"Done: {ok} tickers with data this run; DB now {nt} tickers, {n} rows, {lo} .. {hi}")
    if empty:
        print(f"No data for {len(empty)} tickers (delisted / suspended / not on Yahoo): "
              f"{', '.join(empty[:25])}{' ...' if len(empty) > 25 else ''}")


if __name__ == "__main__":
    main()
