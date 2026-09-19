"""
Standalone historical RS Rating backfill.

Run:  python backfill_rs.py
      python backfill_rs.py --period 1y   # shorter period for a quick smoke test

1. Ensures ticker_universe is populated (refreshes it if empty).
2. Batch-fetches universe price history via yfinance.
3. Computes RS Score/Rating/Line for every trading date now available and
   upserts into rs_ratings (idempotent — safe to re-run).

Separate from the daily incremental update wired into fetch_data.py — this
is for populating history the first time, or re-running after a config
change (e.g. a new RS_BENCHMARK).
"""

import argparse

from db_setup import get_connection, setup_database
from universe import get_active_universe, refresh_universe
from rs_calculator import fetch_universe_prices, compute_rs_ratings


def run(period: str = "2y"):
    setup_database()

    with get_connection() as conn:
        universe = get_active_universe(conn)
        if not universe:
            print("No active universe — refreshing from Wikipedia…")
            refresh_universe(conn, progress_cb=print)
            universe = get_active_universe(conn)

    if not universe:
        print("Universe is still empty after refresh — aborting.")
        return

    print(f"Universe: {len(universe)} tickers. Fetching price history (period={period})…")
    fetch_universe_prices(universe, period=period, progress_cb=print)

    print("Computing RS Score/Rating/Line for all available dates…")
    with get_connection() as conn:
        summary = compute_rs_ratings(conn, dates=None, progress_cb=print)

    print(
        f"\nBackfill complete — {summary['dates_processed']} date(s) processed, "
        f"{summary['rows_written']} rows written, "
        f"{summary['tickers_skipped_no_data']} ticker(s) skipped (no price data)."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill historical RS Ratings")
    parser.add_argument("--period", default="2y",
                        help="yfinance history period for universe prices (default: 2y)")
    args = parser.parse_args()
    run(period=args.period)
