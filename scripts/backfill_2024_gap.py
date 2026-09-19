"""
Backfill the early-history gap (GAP_START .. first date already in the DB) for
BOTH price tables so MA200 / 52wk / Trend Template values become valid earlier:

  - stocks_daily    (tracked extraction_tickers)
  - universe_prices (active S&P 500 + Russell 1000 universe + RS benchmark)

Network footprint is deliberately small: one batched yf.download() per 200
tickers for only the gap window (~6 months of daily bars), not years of data.

After the download, indicators are recomputed LOCALLY from the full stored
history (no further network calls) using the same code paths fetch_data.py
uses, then RS Rank / Trend Template are re-run for both tables.

Safe to re-run: every write is an upsert keyed on (ticker, date).
Historical rs_ratings (rs_calculator.compute_rs_ratings) are NOT recomputed
here -- run backfill_rs.py afterwards if you want RS Ratings for the new dates.
"""
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rs_calculator
import universe
from db_setup import get_connection, setup_database
from fetch_data import (
    _calculate_indicators, _upsert_daily_rows, _compute_universe_indicators,
    _compute_rs_rank, _compute_trend_template,
)

GAP_START = "2024-01-01"
CHUNK_SIZE = 200


def emit(msg):
    print(msg, flush=True)


def _existing_ohlcv(conn, ticker):
    df = pd.read_sql_query(
        "SELECT date, open, high, low, close, volume FROM stocks_daily "
        "WHERE ticker = ? ORDER BY date", conn, params=(ticker,),
    )
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                            "close": "Close", "volume": "Volume"})
    df.index = pd.to_datetime(df.pop("date"))
    return df


def main():
    setup_database()
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t0 = time.time()

    with get_connection() as conn:
        # yfinance `end` is exclusive: stopping at the first stored date means
        # the download never overlaps (or overwrites) existing rows.
        gap_end = conn.execute("SELECT MIN(date) FROM stocks_daily").fetchone()[0]
        tracked = [r["ticker"] for r in conn.execute(
            "SELECT ticker FROM extraction_tickers ORDER BY ticker")]
        tracked = [t for t in tracked if ":" not in t]
        active = universe.get_active_universe(conn)
        in_uni_table = {r["ticker"] for r in conn.execute(
            "SELECT DISTINCT ticker FROM universe_prices")}
        conn.commit()

    uni_tickers = sorted(set(active) | in_uni_table | {rs_calculator.RS_BENCHMARK})
    all_tickers = sorted(set(tracked) | set(uni_tickers))
    emit(f"Backfilling {len(all_tickers)} tickers ({len(tracked)} tracked, "
         f"{len(uni_tickers)} universe), {GAP_START} .. {gap_end} (exclusive)")

    gap_frames = {}   # tracked ticker -> gap OHLCV frame
    chunks = [all_tickers[i:i + CHUNK_SIZE] for i in range(0, len(all_tickers), CHUNK_SIZE)]
    uni_set, tracked_set = set(uni_tickers), set(tracked)
    uni_rows = 0

    with get_connection() as conn:
        for i, chunk in enumerate(chunks, 1):
            try:
                df = yf.download(chunk, start=GAP_START, end=gap_end, group_by="ticker",
                                 threads=True, auto_adjust=True, progress=False)
            except Exception as exc:
                emit(f"chunk {i}/{len(chunks)} FAILED -- {exc}")
                continue
            if df is None or df.empty:
                emit(f"chunk {i}/{len(chunks)}: empty")
                continue

            n = rs_calculator._upsert_prices_from_download(
                conn, df, [t for t in chunk if t in uni_set], fetched_at)
            conn.commit()
            uni_rows += n

            is_multi = isinstance(df.columns, pd.MultiIndex)
            for t in chunk:
                if t not in tracked_set:
                    continue
                if is_multi:
                    if t not in df.columns.get_level_values(0):
                        continue
                    sub = df[t]
                else:
                    sub = df
                sub = sub[sub["Close"].notna()]
                if not sub.empty:
                    gap_frames[t] = sub[["Open", "High", "Low", "Close", "Volume"]]
            emit(f"chunk {i}/{len(chunks)} ({len(chunk)} tickers) -- "
                 f"{n} universe rows ({time.time() - t0:.0f}s)")

    emit(f"Download done. universe_prices rows upserted: {uni_rows}; "
         f"tracked tickers with gap data: {len(gap_frames)}")

    emit("Recomputing tracked-ticker indicators from full history (local)...")
    with get_connection() as conn:
        for t, gap in gap_frames.items():
            full = pd.concat([gap, _existing_ohlcv(conn, t)])
            full = full[~full.index.duplicated(keep="last")].sort_index()
            _upsert_daily_rows(conn, t, _calculate_indicators(full), fetched_at)
        conn.commit()

        _compute_universe_indicators(
            conn, [t for t in uni_tickers if t != rs_calculator.RS_BENCHMARK], emit)
        conn.commit()

        for table in ("stocks_daily", "universe_prices"):
            _compute_rs_rank(conn, emit, table=table)
            conn.commit()
        for table in ("stocks_daily", "universe_prices"):
            _compute_trend_template(conn, emit, table=table)
            conn.commit()

    emit(f"Done in {time.time() - t0:.0f}s.")


if __name__ == "__main__":
    main()
