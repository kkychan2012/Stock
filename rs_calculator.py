"""
rs_calculator.py — IBD-style Relative Strength (RS) Rating engine.

Methodology (approximates IBD's published RS Rating):
  1. RS Score = weighted price performance vs. N trading days ago:
         0.4 * (P_now / P_3mo_ago)  +  0.2 * (P_now / P_6mo_ago)
       + 0.2 * (P_now / P_9mo_ago)  +  0.2 * (P_now / P_12mo_ago)
     using 63/126/189/252 trading days as 3/6/9/12-month proxies. A stock
     needs 252+ trading days of history as of a given date or its RS Score
     for that date is left as NULL (skipped, not errored).
  2. RS Rating = cross-sectional percentile (1-99) of RS Score against every
     other stock in the active universe (universe.py) on that same date —
     this is a batch step, never computed for a single stock in isolation.
  3. RS Line = Close / Benchmark Close (default ^GSPC), a full time series
     per ticker for charting; RS Line trend is 'up'/'down' vs. 20 trading
     days ago.

Limitations vs. the official IBD RS Rating:
  - Percentile is relative to the tracked S&P 500 + Russell 1000 universe
    (~1,000 stocks), not IBD's full ~9,000-stock US market — ratings will
    run systematically a bit higher/lower than IBD's published number for
    the same stock.
  - Index membership is applied retroactively: a historical backfill uses
    *today's* S&P 500 + Russell 1000 constituents, not the point-in-time
    membership on each historical date (no historical reconstitution data
    is scraped) — this is a known survivorship-style bias in the backfill.
  - IBD's exact weighting/smoothing formula is proprietary; this is a
    standard public approximation of it.

Run standalone for a quick sanity-check:
  python rs_calculator.py
"""

from datetime import datetime

import pandas as pd
import yfinance as yf

from db_setup import get_connection, setup_database
from universe import get_active_universe

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RS_BENCHMARK = "^GSPC"   # default RS Line benchmark; alternates: 'QQQ', 'IWM'

RS_WEIGHTS = {63: 0.4, 126: 0.2, 189: 0.2, 252: 0.2}   # 3/6/9/12-month proxies
MIN_HISTORY_WINDOW = 252                                # trading days required
RS_LINE_TREND_LOOKBACK = 20                             # trading days

CHUNK_SIZE = 200   # tickers per yf.download() batch call


def _emit(progress_cb, msg: str):
    if progress_cb:
        progress_cb(msg)
    else:
        print(msg, flush=True)


# ---------------------------------------------------------------------------
# Part 0/5 support: batched universe price fetch
# ---------------------------------------------------------------------------

def _upsert_prices_from_download(conn, df, symbols, fetched_at: str) -> int:
    if df is None or df.empty:
        return 0
    is_multi = isinstance(df.columns, pd.MultiIndex)
    rows = []
    for sym in symbols:
        try:
            if is_multi:
                if sym not in df.columns.get_level_values(0):
                    continue
                sub = df[sym]
            else:
                sub = df
            if "Close" not in sub.columns:
                continue
            sub = sub[sub["Close"].notna()]
            for row in sub.itertuples():
                rows.append((
                    sym, pd.Timestamp(row.Index).strftime("%Y-%m-%d"),
                    float(row.Close),
                    _up_safe(getattr(row, "Open", None)),
                    _up_safe(getattr(row, "High", None)),
                    _up_safe(getattr(row, "Low", None)),
                    _up_safe_int(getattr(row, "Volume", None)),
                    fetched_at,
                ))
        except Exception:
            continue
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO universe_prices (ticker, date, close, open, high, low, volume, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, date) DO UPDATE SET
            close = excluded.close,
            open = excluded.open, high = excluded.high, low = excluded.low,
            volume = excluded.volume,
            fetched_at = excluded.fetched_at
        """,
        rows,
    )
    return len(rows)


def _up_safe(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return round(float(val), 6)
    except (TypeError, ValueError):
        return None


def _up_safe_int(val):
    try:
        if val is None or pd.isna(val):
            return None
        return int(val)
    except (TypeError, ValueError):
        return None


def fetch_universe_prices(tickers: list[str], benchmark: str = RS_BENCHMARK,
                           period: str = "2y", progress_cb=None,
                           chunk_size: int = CHUNK_SIZE) -> int:
    """Batched yf.download() for the universe price cache. Chunked +
    try/except per chunk so one bad batch doesn't abort the run. Returns
    total rows upserted.

    Does not call setup_database() itself — every caller (fetch_data.py,
    backfill_rs.py, this module's own __main__ smoke test) already calls it
    before reaching here. Calling it again here used to open a second
    connection and crash with "database is locked" whenever this ran from
    inside fetch_all()'s outer connection, which already holds an uncommitted
    write transaction from _compute_rs_rank by this point."""
    symbols = list(dict.fromkeys(tickers))   # de-dup, preserve order
    fetched_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    total_rows = 0

    with get_connection() as conn:
        try:
            bdf = yf.download([benchmark], period=period, group_by="ticker",
                               threads=True, auto_adjust=True, progress=False)
            n = _upsert_prices_from_download(conn, bdf, [benchmark], fetched_at)
            conn.commit()
            total_rows += n
            _emit(progress_cb, f"rs_calculator: benchmark {benchmark} — {n} rows")
        except Exception as exc:
            _emit(progress_cb, f"rs_calculator: benchmark {benchmark} fetch failed — {exc}")

        chunks = [symbols[i:i + chunk_size] for i in range(0, len(symbols), chunk_size)]
        for i, chunk in enumerate(chunks, 1):
            try:
                df = yf.download(chunk, period=period, group_by="ticker",
                                  threads=True, auto_adjust=True, progress=False)
                n = _upsert_prices_from_download(conn, df, chunk, fetched_at)
                conn.commit()
                total_rows += n
                _emit(progress_cb,
                      f"rs_calculator: universe prices chunk {i}/{len(chunks)} "
                      f"({len(chunk)} tickers) — {n} rows")
            except Exception as exc:
                _emit(progress_cb, f"rs_calculator: chunk {i}/{len(chunks)} failed — {exc}")

    _emit(progress_cb, f"rs_calculator: universe price fetch done — {total_rows} rows upserted")
    return total_rows


# ---------------------------------------------------------------------------
# Price matrix
# ---------------------------------------------------------------------------

def _load_price_matrix(conn, tickers: list[str], benchmark: str = RS_BENCHMARK):
    """Pivot universe_prices into a date x ticker Close matrix (one query,
    one pivot) plus the benchmark's own Close series."""
    query_tickers = list(dict.fromkeys(tickers + [benchmark]))
    placeholders = ",".join("?" * len(query_tickers))
    df = pd.read_sql_query(
        f"SELECT ticker, date, close FROM universe_prices WHERE ticker IN ({placeholders})",
        conn, params=query_tickers,
    )
    if df.empty:
        return pd.DataFrame(), pd.Series(dtype=float)

    df["date"] = pd.to_datetime(df["date"])
    pivot = df.pivot_table(index="date", columns="ticker", values="close").sort_index()

    if benchmark in pivot.columns:
        benchmark_series = pivot.pop(benchmark)
    else:
        benchmark_series = pd.Series(dtype=float, index=pivot.index)

    prices = pivot.reindex(columns=[t for t in tickers if t in pivot.columns])
    return prices, benchmark_series


def _weighted_rs_score(prices: pd.DataFrame) -> pd.DataFrame:
    """Weighted 3/6/9/12-month pct-change score, vectorized across the whole
    date x ticker matrix. NULL wherever the 252-day window isn't available
    yet (Part 1's 'skip insufficient-history stocks' rule)."""
    score_num    = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    weight_avail = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    for window, w in RS_WEIGHTS.items():
        p = prices.pct_change(window)
        valid = p.notna()
        score_num    += p.fillna(0) * w
        weight_avail += valid.astype(float) * w

    rs_score = (score_num / weight_avail).where(weight_avail > 0)
    valid_full_window = prices.pct_change(MIN_HISTORY_WINDOW).notna()
    return rs_score.where(valid_full_window)


# ---------------------------------------------------------------------------
# Cross-sectional RS Rating + RS Line (Parts 2-5, shared by backfill + daily)
# ---------------------------------------------------------------------------

def compute_rs_ratings(conn, dates: list[str] | None = None,
                        benchmark: str = RS_BENCHMARK, progress_cb=None) -> dict:
    """Compute RS Score/Rating/Line for every date in `dates` (or every date
    present in the price matrix if dates=None — full backfill). Upserts into
    rs_ratings. Shared engine for both backfill_rs.py and the daily
    incremental update in fetch_data.py."""
    active = get_active_universe(conn)
    if not active:
        _emit(progress_cb, "rs_calculator: no active universe — skipping RS calc")
        return {"dates_processed": 0, "rows_written": 0, "tickers_skipped_no_data": 0}

    prices, bench = _load_price_matrix(conn, active, benchmark)
    if prices.empty or bench.empty:
        _emit(progress_cb, "rs_calculator: no price data available — skipping RS calc")
        return {"dates_processed": 0, "rows_written": 0, "tickers_skipped_no_data": len(active)}

    tickers_with_data = set(prices.columns)
    skipped_no_data = [t for t in active if t not in tickers_with_data]

    rs_score_matrix = _weighted_rs_score(prices)
    rs_line_matrix  = prices.div(bench, axis=0)

    target_set = set(dates) if dates is not None else None
    idx_list = list(prices.index)
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    upserts = []
    dates_processed = 0

    for i, dt in enumerate(idx_list):
        date_str = dt.strftime("%Y-%m-%d")
        if target_set is not None and date_str not in target_set:
            continue

        row_scores = rs_score_matrix.loc[dt].dropna()
        if len(row_scores) < 2:
            continue

        pct_rank = (row_scores.rank(pct=True) * 98 + 1).round().clip(1, 99).astype(int)

        row_rs_line = rs_line_matrix.loc[dt]
        prior_idx = i - RS_LINE_TREND_LOOKBACK
        prior_rs_line = rs_line_matrix.iloc[prior_idx] if prior_idx >= 0 else None

        for ticker, rating in pct_rank.items():
            rs_line_val = row_rs_line.get(ticker)
            rs_line_val = None if pd.isna(rs_line_val) else float(rs_line_val)

            trend = None
            if prior_rs_line is not None and rs_line_val is not None:
                prior_val = prior_rs_line.get(ticker)
                if pd.notna(prior_val):
                    trend = "up" if rs_line_val > prior_val else "down"

            rs_leader = 1 if (rating >= 70 and trend == "up") else 0
            upserts.append((
                ticker, date_str, float(row_scores[ticker]), int(rating),
                rs_line_val, trend, rs_leader, now_ts,
            ))

        dates_processed += 1
        if dates_processed % 20 == 0:
            _emit(progress_cb, f"rs_calculator: processed {dates_processed} date(s)…")

    if upserts:
        conn.executemany(
            """
            INSERT INTO rs_ratings
                (ticker, date, rs_score, rs_rating, rs_line_value, rs_line_trend, rs_leader, created_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker, date) DO UPDATE SET
                rs_score      = excluded.rs_score,
                rs_rating     = excluded.rs_rating,
                rs_line_value = excluded.rs_line_value,
                rs_line_trend = excluded.rs_line_trend,
                rs_leader     = excluded.rs_leader,
                created_at    = excluded.created_at
            """,
            upserts,
        )
        conn.commit()

    if target_set is not None and dates_processed == 0:
        _emit(progress_cb, f"rs_calculator: no matching trading date(s) in price data for {sorted(target_set)}")

    _emit(
        progress_cb,
        f"rs_calculator: {dates_processed} date(s) processed, {len(upserts)} rows written, "
        f"{len(skipped_no_data)} ticker(s) skipped (no price data)",
    )
    return {
        "dates_processed": dates_processed,
        "rows_written": len(upserts),
        "tickers_skipped_no_data": len(skipped_no_data),
    }


if __name__ == "__main__":
    setup_database()
    tickers = get_active_universe()
    print(f"Active universe: {len(tickers)} tickers")
    if tickers:
        fetch_universe_prices(tickers[:50], period="1y", progress_cb=print)
        with get_connection() as conn:
            summary = compute_rs_ratings(conn, dates=None, progress_cb=print)
        print(summary)
