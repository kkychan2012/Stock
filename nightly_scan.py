"""
nightly_scan.py — nightly price update + Surge scan + Pattern scan (all 9 patterns).

Run by the Windows scheduled task "StockDashboard Nightly Scan" (Mon-Fri 23:15
UK time, pythonw, start-when-available), independent of the Morning Brief:
  1. waits for nightly_collect.py (23:00) to finish, max 90 min — avoids two
     jobs writing the SQLite DB at once;
  2. quick US price update (fetch_data.quick_update, last ~20 days, RS +
     Trend Template refreshed) — so the session that just closed is stored;
  3. Surge strategy scan (surge_signals);
  4. Pattern scan for the latest complete session -> pattern_scan_results +
     vcp_signals (the Pattern Scanner tab's tables), default thresholds;
  5. Factor & Sector Rotation refresh (rotation.refresh(): ~23 ETFs, incremental).

One row per run in data_collection_runs (job 'pattern_scan'). The Morning Brief
(23:30) waits for this job and skips its own update + scan when it succeeded;
if it didn't run, the brief still does both itself.

Lock: logs/nightly_scan.lock holds the PID (stale = PID gone). Log: logs/nightly_scan.log.
CLI: python nightly_scan.py [--as-of YYYY-MM-DD] [--no-update]
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

from db_setup import get_connection, setup_database  # noqa: E402

LOG_DIR = os.path.join(BASE_DIR, "logs")
LOCK_FILE = os.path.join(LOG_DIR, "nightly_scan.lock")
os.makedirs(LOG_DIR, exist_ok=True)

log = logging.getLogger("nightly_scan")
if not log.handlers:
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(LOG_DIR, "nightly_scan.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)
    if sys.stdout is not None:               # pythonw has no console
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log.addHandler(sh)


def _acquire_lock():
    from nightly_collect import _pid_alive
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                pid = int(open(LOCK_FILE).read().strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if _pid_alive(pid):
                log.info("Another nightly_scan (PID %s) is running — exiting.", pid)
                return False
            try:
                os.remove(LOCK_FILE)          # stale
            except OSError:
                pass
    return False


def run(as_of=None, update=True):
    import daily_brief
    started = datetime.now().isoformat(timespec="seconds")
    detail, status = {}, "ok"
    t0 = time.time()
    try:
        setup_database()
        if update:
            daily_brief.wait_for_collector()
            t1 = time.time()
            try:
                daily_brief.refresh_data(log.info)       # quick update + Surge scan
                detail["update_sec"] = round(time.time() - t1)
            except Exception as exc:
                log.exception("price update failed (scanning stored data): %s", exc)
                detail["update_error"] = f"{type(exc).__name__}: {exc}"
        as_of = as_of or daily_brief.latest_complete_date()
        detail["as_of"] = as_of
        log.info("Pattern scan for %s ...", as_of)
        import scan_patterns
        scan_patterns.scan_all_patterns(as_of)
        with get_connection() as conn:
            detail["pattern_hits"] = conn.execute(
                "SELECT COUNT(*) FROM pattern_scan_results WHERE scan_date=?", (as_of,)).fetchone()[0]
            detail["vcp_detected"] = conn.execute(
                "SELECT COUNT(*) FROM vcp_signals WHERE scan_date=? AND vcp_detected=1", (as_of,)).fetchone()[0]
        log.info("Pattern scan for %s done: %s hits, %s VCP.", as_of, detail["pattern_hits"], detail["vcp_detected"])
        if update:
            try:                                       # Factor & Sector Rotation ETFs (~23 tickers, seconds)
                import rotation
                r = rotation.refresh()
                detail["rotation"] = {k: r.get(k) for k in ("status", "as_of", "failed", "rebackfilled")}
                log.info("Rotation refresh: %s", detail["rotation"])
            except Exception as exc:
                log.exception("rotation refresh failed: %s", exc)
                detail["rotation_error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        status = "error"
        detail["error"] = f"{type(exc).__name__}: {exc}"
        log.exception("nightly scan failed: %s", exc)
    detail["minutes"] = round((time.time() - t0) / 60, 1)
    with get_connection() as conn:
        conn.execute("INSERT INTO data_collection_runs (started_at, finished_at, job, status, detail) "
                     "VALUES (?,?,?,?,?)",
                     (started, datetime.now().isoformat(timespec="seconds"), "pattern_scan", status,
                      json.dumps(detail)))
        conn.commit()
    return status == "ok"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Nightly price update + Surge scan + Pattern scan")
    ap.add_argument("--as-of", help="YYYY-MM-DD session to scan (default: latest complete)")
    ap.add_argument("--no-update", action="store_true", help="skip the price update / Surge scan")
    a = ap.parse_args()
    if not _acquire_lock():
        sys.exit(0)
    try:
        ok = run(as_of=a.as_of, update=not a.no_update)
    finally:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
    sys.exit(0 if ok else 1)
