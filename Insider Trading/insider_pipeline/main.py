"""
Entry point for the insider trading pipeline.

Usage:
  python main.py                               # full 180-day market scan
  python main.py --days 7                      # last 7 days
  python main.py --ticker AAPL                 # single stock (fast)
  python main.py --ticker AAPL,MSFT,NVDA       # watchlist
  python main.py --from 2026-01-01 --to 2026-06-08
  python main.py --schedule                    # daily at 18:00
  python main.py --gui                         # launch GUI
"""
import argparse
import logging
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import schedule
from tqdm import tqdm

import config
from sec_fetcher import (
    fetch_form4_index,
    fetch_form4_xml,
    fetch_form4_xml_with_doc,
    fetch_form4_by_company,
    fetch_cik_for_ticker,
)
from parser import parse_form4
from form6k_fetcher import fetch_form6k_by_company, fetch_form6k_document
from form6k_parser import parse_form6k
from filter import apply_filters
from cluster_detector import detect_clusters
from excel_exporter import export_to_excel

MAX_WORKERS = 8

# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging(extra_handler=None):
    handlers = [
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    if extra_handler:
        handlers.append(extra_handler)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )

logger = logging.getLogger(__name__)


# ── Core pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(
    date_from: datetime,
    date_to: datetime,
    tickers: list = None,
    progress_cb=None,   # callable(done: int, total: int)
    stop_event=None,    # threading.Event — set to request cancellation
):
    """
    Run the full pipeline.  Two modes:
    - Ticker mode  (tickers is a list): fast per-company lookup via submissions API
    - Market mode  (tickers is None):   full EFTS index scan

    Returns (df, scanned_count).
    """
    logger.info("=" * 60)
    logger.info("Pipeline start  %s -> %s", date_from.date(), date_to.date())
    if tickers:
        logger.info("Mode: watchlist  %s", ", ".join(tickers))
    else:
        logger.info("Mode: full market scan")
    logger.info("=" * 60)

    # ── Step 1: build filing list ─────────────────────────────────────────────
    logger.info("Step 1/5: Fetching Form 4 filing index ...")
    filings: list[dict] = []
    form6k_records_prefetch: list[dict] = []   # 6-K records collected during Step 1
    form6k_filings_count: int = 0

    if tickers:
        for ticker in tickers:
            if stop_event and stop_event.is_set():
                break
            cik = fetch_cik_for_ticker(ticker)
            if not cik:
                logger.warning("Ticker not found in EDGAR: %s", ticker)
                continue
            batch = fetch_form4_by_company(cik, date_from, date_to)
            if batch:
                filings.extend(batch)
            else:
                # No Form 4 filings — likely a foreign private issuer; try Form 6-K
                logger.info("  No Form 4 for %s — foreign issuer detected, scanning Form 6-K ...", ticker)
                form6k_filings = fetch_form6k_by_company(cik, date_from, date_to)
                form6k_filings_count += len(form6k_filings)
                logger.info("  Found %d Form 6-K filings for %s", len(form6k_filings), ticker)
                fetched, keyword_miss, parsed_ok = 0, 0, 0
                for f6k in form6k_filings:
                    if stop_event and stop_event.is_set():
                        break
                    try:
                        html = fetch_form6k_document(f6k)
                        if not html:
                            logger.warning("  6-K doc not fetched: %s  primary_doc=%s",
                                           f6k.get("accession"), f6k.get("primary_doc"))
                            continue
                        fetched += 1
                        recs = parse_form6k(
                            html, f6k["cik"], f6k["accession"],
                            f6k["filed_date"], f6k["company_name"],
                            ticker=ticker, issuer_cik=f6k.get("issuer_cik"),
                        )
                        if recs:
                            parsed_ok += len(recs)
                            form6k_records_prefetch.extend(recs)
                        else:
                            keyword_miss += 1
                            logger.debug("  6-K no manager keywords: %s  date=%s",
                                         f6k.get("accession"), f6k.get("filed_date"))
                    except Exception as exc:
                        logger.warning("6-K parse error %s: %s", f6k.get("accession"), exc)
                logger.info("  6-K summary for %s: fetched=%d  keyword_miss=%d  transactions=%d",
                            ticker, fetched, keyword_miss, parsed_ok)
                if form6k_records_prefetch:
                    logger.info("  Parsed %d manager transactions from Form 6-K for %s",
                                len(form6k_records_prefetch), ticker)
                else:
                    logger.info("  No qualifying manager transactions found in Form 6-K for %s", ticker)
    else:
        filings = fetch_form4_index(date_from, date_to)

    scanned_count = len(filings) + form6k_filings_count
    logger.info("  Found %d Form 4 filings + %d Form 6-K filings (%d transactions parsed)",
                len(filings), form6k_filings_count, len(form6k_records_prefetch))

    if stop_event and stop_event.is_set():
        logger.info("Scan cancelled by user.")
        return apply_filters([]), 0

    # ── Step 2: download + parse ──────────────────────────────────────────────
    logger.info("Step 2/5: Downloading and parsing XML filings ...")
    all_records: list[dict] = []
    done_count = 0

    def _process(filing: dict) -> list[dict]:
        if stop_event and stop_event.is_set():
            return []
        cik = filing["cik"]
        accession = filing["accession"]
        if not cik or not accession:
            return []
        try:
            issuer_cik = filing.get("issuer_cik")
            primary_doc = filing.get("primary_doc")
            if primary_doc:
                xml_text = fetch_form4_xml_with_doc(cik, accession, issuer_cik, primary_doc)
            else:
                xml_text = fetch_form4_xml(cik, accession, issuer_cik=issuer_cik)
            if not xml_text:
                return []
            return parse_form4(
                xml_text, cik, accession,
                filing["filed_date"], filing["company_name"], ticker=None,
                issuer_cik=filing.get("issuer_cik"),
            )
        except Exception as exc:
            logger.warning("Skipping %s/%s: %s", cik, accession, exc)
            return []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_process, f): f for f in filings}
        with tqdm(total=len(filings), desc="Parsing filings", unit="filing") as pbar:
            for fut in as_completed(futures):
                records = fut.result()
                all_records.extend(records)
                done_count += 1
                pbar.update(1)
                if progress_cb:
                    progress_cb(done_count, scanned_count)

    # Merge any Form 6-K records collected during Step 1
    all_records.extend(form6k_records_prefetch)
    logger.info("  Parsed %d raw purchase transactions (incl. %d from Form 6-K)",
                len(all_records), len(form6k_records_prefetch))

    if stop_event and stop_event.is_set():
        logger.info("Scan cancelled — exporting partial results.")

    # ── Step 3: filter ────────────────────────────────────────────────────────
    logger.info("Step 3/5: Applying high-conviction filters ...")
    df = apply_filters(all_records)
    logger.info("  %d transactions pass filters", len(df))

    # ── Step 4: cluster detection ─────────────────────────────────────────────
    logger.info("Step 4/5: Detecting cluster buys ...")
    if not df.empty:
        df = detect_clusters(df)
        cluster_count = int(df["cluster_buy"].sum())
        logger.info("  %d cluster-buy transactions", cluster_count)
    else:
        cluster_count = 0

    # ── Step 5: export ────────────────────────────────────────────────────────
    logger.info("Step 5/5: Exporting to Excel ...")
    output_path = export_to_excel(df, scanned_count)

    print("\n" + "=" * 60)
    print(f"  Scanned {scanned_count:,} filings")
    print(f"  Found   {len(df):,} qualifying buys")
    print(f"  Cluster {cluster_count:,} cluster-buy signals")
    print(f"  Output  {output_path}")
    print("=" * 60 + "\n")

    return df, scanned_count


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Insider trading pipeline")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--from", dest="date_from", type=str)
    p.add_argument("--to", dest="date_to", type=str)
    p.add_argument("--ticker", type=str, default=None,
                   help="Comma-separated tickers, e.g. AAPL,MSFT")
    p.add_argument("--schedule", action="store_true")
    p.add_argument("--gui", action="store_true", help="Launch GUI")
    return p.parse_args()


def main():
    args = parse_args()

    if args.gui:
        setup_logging()
        from gui import launch_gui
        launch_gui()
        return

    setup_logging()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    if args.date_from and args.date_to:
        date_from = datetime.strptime(args.date_from, "%Y-%m-%d")
        date_to = datetime.strptime(args.date_to, "%Y-%m-%d")
    elif args.days:
        date_from = today - timedelta(days=args.days)
        date_to = today
    else:
        date_from = today - timedelta(days=config.LOOKBACK_DAYS)
        date_to = today

    tickers = None
    if args.ticker:
        tickers = [t.strip().upper() for t in args.ticker.split(",") if t.strip()]

    if args.schedule:
        logger.info("Scheduler mode: running now, then daily at 18:00 ...")
        run_pipeline(date_from, date_to, tickers=tickers)

        def daily_job():
            t = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            run_pipeline(t - timedelta(days=1), t)

        schedule.every().day.at("18:00").do(daily_job)
        while True:
            schedule.run_pending()
            time.sleep(30)
    else:
        run_pipeline(date_from, date_to, tickers=tickers)


if __name__ == "__main__":
    main()
