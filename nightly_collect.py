"""
nightly_collect.py — nightly data collection job (Windows Task Scheduler, 23:00).

Collects and stores in stock_dashboard.db, without the dashboard needing to run:

  1. Congress / political trades  — House + Senate PTRs and executive branch
     (President, Vice President, Cabinet ... OGE 278-T) via
     congress_trades.fetch_congress_trades()  -> congress_trades
     The upstream feed is a rolling snapshot, so collecting it every night is
     what builds up a lasting history.
  2. SEC Form 4 insider trades, WHOLE US market (every company that files
     Form 4, not just a ticker list), via the insider pipeline in market mode
     with --store-all:
       - insider_trades_all : every transaction (every role incl. 10% owners,
                              every transaction code, any size) - complete store
       - insider_signals    : the filtered high-conviction buys/sells the
                              Insider tab shows (officers/directors, >= $100k)
     Then cluster flags on insider_signals are recomputed over the whole table,
     so clusters spanning several nightly runs are still caught.
  3. SEC Form 144 (proposed insider sales) and Schedule 13D/13G (> 5% holders)
     for the whole market, last 3 filing days, via sec_filings.collect()
     -> sec_form144, sec_13dg

The SEC steps run one after another (never in parallel) to stay inside the
SEC's 10 requests/second limit.

Each run is logged to logs/nightly_collect.log and recorded in the
data_collection_runs table. Re-running is always safe (INSERT OR IGNORE).

Usage:
  python nightly_collect.py                     # what the scheduled task runs
  python nightly_collect.py --insider-days 30   # wider insider window (backfill)
  python nightly_collect.py --skip-insider | --skip-congress | --skip-sec
  python nightly_collect.py --backfill-days 183 --skip-congress      # insider history
  python nightly_collect.py --backfill-sec-days 183 --skip-congress  # 144 + 13D/G history
  --wait-lock-hours N : wait for a running job instead of exiting (scheduled task: 16)
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from db_setup import get_connection, setup_database  # noqa: E402

PIPELINE = os.path.join(ROOT, "Insider Trading", "insider_pipeline", "main.py")
LOG_DIR = os.path.join(ROOT, "logs")
LOCK_FILE = os.path.join(LOG_DIR, "nightly_collect.lock")

# Filing-date window for the nightly insider scan. A few days of overlap
# catches filings EDGAR indexes late; duplicates are ignored on insert.
DEFAULT_INSIDER_DAYS = 3
INSIDER_TIMEOUT_S = 6 * 3600

CLUSTER_WINDOW_DAYS = 7     # same thresholds as insider_pipeline/config.py
CLUSTER_MIN_INSIDERS = 3

log = logging.getLogger("nightly")


def _setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    handlers = [logging.FileHandler(os.path.join(LOG_DIR, "nightly_collect.log"), encoding="utf-8")]
    if sys.stdout is not None:          # None under pythonw.exe (scheduled task, no window)
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=handlers)


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID is running (Windows via ctypes)."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return code.value == 259                        # STILL_ACTIVE


def _acquire_lock(wait_hours: float = 0) -> bool:
    """One run at a time. A lock whose PID is no longer running is stale.
    With wait_hours > 0, wait (checking every minute) for the other run to
    finish instead of exiting - the scheduled task uses this so a long
    backfill delays the nightly run rather than cancelling it."""
    deadline = time.time() + wait_hours * 3600
    announced = False
    while True:
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            pass
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip() or 0)
        except (OSError, ValueError):
            pid = 0
        if not _pid_alive(pid):
            log.warning("Removing stale lock (PID %s not running).", pid)
            try:
                os.remove(LOCK_FILE)
            except OSError:
                pass
            continue
        if time.time() >= deadline:
            log.warning("Another run (PID %s) is active - exiting.", pid)
            return False
        if not announced:
            log.info("Another run (PID %s) is active - waiting up to %.0f h for it.", pid, wait_hours)
            announced = True
        time.sleep(60)


def _record_run(job, started, status, detail):
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO data_collection_runs (started_at, finished_at, job, status, detail) "
                "VALUES (?,?,?,?,?)",
                (started, datetime.now().isoformat(timespec="seconds"), job, status,
                 json.dumps(detail, default=str)[:4000]),
            )
            conn.commit()
    except Exception as exc:
        log.error("Could not record run: %s", exc)


def run_congress() -> bool:
    started = datetime.now().isoformat(timespec="seconds")
    log.info("== Congress / executive-branch trades ==")
    try:
        from congress_trades import fetch_congress_trades
        summary = fetch_congress_trades(progress_cb=lambda m: log.info("  %s", m))
        _record_run("congress", started, "ok", summary)
        return True
    except Exception as exc:
        log.exception("Congress fetch failed")
        _record_run("congress", started, "error", {"error": str(exc)})
        return False


def _count(table):
    with get_connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _recompute_insider_clusters():
    """Whole-table cluster flags on insider_signals: 3+ distinct insiders,
    same ticker, same direction, transaction dates within 7 days."""
    with get_connection() as conn:
        conn.execute("UPDATE insider_signals SET cluster_buy = 0")
        conn.execute(f"""
            UPDATE insider_signals SET cluster_buy = 1
            WHERE id IN (
                SELECT t1.id
                FROM insider_signals t1
                JOIN insider_signals t2
                  ON t1.ticker = t2.ticker
                 AND t1.transaction_type = t2.transaction_type
                 AND t2.insider_name != t1.insider_name
                 AND ABS(JULIANDAY(t1.transaction_date) - JULIANDAY(t2.transaction_date))
                     <= {CLUSTER_WINDOW_DAYS}
                GROUP BY t1.id
                HAVING COUNT(DISTINCT t2.insider_name) >= {CLUSTER_MIN_INSIDERS - 1}
            )
        """)
        conn.commit()
        return conn.execute("SELECT COUNT(*) FROM insider_signals WHERE cluster_buy = 1").fetchone()[0]


def run_insider(days: int = None, date_from: str = None, date_to: str = None) -> bool:
    """Whole-market Form 4 scan: the last `days` days, or date_from..date_to."""
    started = datetime.now().isoformat(timespec="seconds")
    span = f"last {days} days" if days else f"{date_from} -> {date_to}"
    log.info("== SEC Form 4 insider trades, whole market, %s ==", span)
    before_all, before_sig = _count("insider_trades_all"), _count("insider_signals")
    window = ["--days", str(days)] if days else ["--from", date_from, "--to", date_to]
    cmd = [sys.executable, PIPELINE, *window, "--store-all"]
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            cmd, cwd=os.path.dirname(PIPELINE),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        for line in proc.stdout:
            line = line.rstrip()
            # The pipeline logs every request at DEBUG to its own pipeline.log;
            # only keep the INFO+ lines and the summary here.
            if line and "[DEBUG]" not in line and "Parsing filings:" not in line:
                log.info("  %s", line)
            if time.time() - t0 > INSIDER_TIMEOUT_S:
                proc.kill()
                raise TimeoutError(f"insider pipeline exceeded {INSIDER_TIMEOUT_S // 3600} h")
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"insider pipeline exited with code {proc.returncode}")
        clusters = _recompute_insider_clusters()
        detail = {
            "window": span,
            "minutes": round((time.time() - t0) / 60, 1),
            "new_all_trades": _count("insider_trades_all") - before_all,
            "new_signals": _count("insider_signals") - before_sig,
            "cluster_rows": clusters,
        }
        log.info("  Insider done: %s", detail)
        _record_run("insider", started, "ok", detail)
        return True
    except Exception as exc:
        log.exception("Insider pipeline failed")
        _record_run("insider", started, "error", {"error": str(exc), "window": span})
        return False


BACKFILL_SLICE_DAYS = 14
DEFAULT_SEC_DAYS = 3


def run_sec_filings(date_from, date_to) -> bool:
    """Form 144 + Schedule 13D/13G, whole market (sec_filings.py)."""
    started = datetime.now().isoformat(timespec="seconds")
    log.info("== SEC Form 144 + 13D/13G, whole market, %s -> %s ==", date_from, date_to)
    t0 = time.time()
    try:
        from sec_filings import collect
        stats = collect(date_from, date_to, progress=lambda m: log.info("  %s", m))
        stats["window"] = f"{date_from} -> {date_to}"
        stats["minutes"] = round((time.time() - t0) / 60, 1)
        log.info("  SEC filings done: %s", stats)
        _record_run("sec_144_13dg", started, "ok", stats)
        return True
    except Exception as exc:
        log.exception("Form 144 / 13D/G collection failed")
        _record_run("sec_144_13dg", started, "error",
                    {"error": str(exc), "window": f"{date_from} -> {date_to}"})
        return False


def run_backfill(total_days: int) -> bool:
    """Oldest slice first; each slice is saved before the next starts, so an
    interruption only loses the slice in progress (re-running is safe)."""
    end = datetime.now().date()
    start = end - timedelta(days=total_days)
    ok, cursor = True, start
    while cursor <= end:
        slice_end = min(cursor + timedelta(days=BACKFILL_SLICE_DAYS - 1), end)
        ok &= run_insider(date_from=cursor.isoformat(), date_to=slice_end.isoformat())
        cursor = slice_end + timedelta(days=1)
    return ok


def main():
    ap = argparse.ArgumentParser(description="Nightly insider / Congress / Form 144 / 13D-13G data collection")
    ap.add_argument("--insider-days", type=int, default=DEFAULT_INSIDER_DAYS)
    ap.add_argument("--skip-insider", action="store_true")
    ap.add_argument("--skip-congress", action="store_true")
    ap.add_argument("--sec-days", type=int, default=DEFAULT_SEC_DAYS,
                    help="Form 144 + 13D/13G window in days (filing date)")
    ap.add_argument("--skip-sec", action="store_true")
    ap.add_argument("--backfill-sec-days", type=int, default=None,
                    help="One-off Form 144 + 13D/13G backfill over this many past days")
    ap.add_argument("--wait-lock-hours", type=float, default=0,
                    help="If another run is active, wait this long instead of exiting")
    ap.add_argument("--backfill-days", type=int, default=None,
                    help="One-off insider history backfill: scan this many past days in "
                         "BACKFILL_SLICE_DAYS slices, oldest first, saving after each slice")
    args = ap.parse_args()

    _setup_logging()
    if not _acquire_lock(args.wait_lock_hours):
        sys.exit(2)
    ok = True
    try:
        log.info("=" * 60)
        log.info("Nightly collection start")
        setup_database()
        if not args.skip_congress:
            ok &= run_congress()
        today = datetime.now().date()
        backfill = bool(args.backfill_days or args.backfill_sec_days)
        if args.backfill_days:
            ok &= run_backfill(args.backfill_days)
        elif not args.skip_insider and not backfill:
            ok &= run_insider(days=args.insider_days)
        if args.backfill_sec_days:
            ok &= run_sec_filings(today - timedelta(days=args.backfill_sec_days), today)
        elif not args.skip_sec and not backfill:
            ok &= run_sec_filings(today - timedelta(days=args.sec_days), today)
        log.info("Nightly collection finished: %s", "OK" if ok else "WITH ERRORS")
    finally:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
