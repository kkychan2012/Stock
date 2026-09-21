"""
Parity check: the live engine (surge_strategy.replay, rule ENTRY_RULE="touch") must find exactly the same
Scenario A entries as the backtest study code (scripts/study_volume_surge_2026_04.py, ENTRY_MODE="touch"),
run over the same stored data: same ticker + surge day + entry day.

Both sides use the same rule: after the surge day and the first red candle, a day triggers only if its range
[Low, High] touches the surge-day close within 5 trading days of the drop day.

Run from the project root:  python scripts/verify_surge_engine.py
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from db_setup import get_connection
import surge_strategy as ss
import study_volume_surge_2026_04 as study

FIRST, LAST = "2024-11", "2026-09"


def months():
    y, m = int(FIRST[:4]), int(FIRST[5:7])
    ly, lm = int(LAST[:4]), int(LAST[5:7])
    while (y, m) <= (ly, lm):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            y, m = y + 1, 1


def main():
    study.ENTRY_MODE = "touch"
    conn = sqlite3.connect(study.DB_PATH)
    study_series = study.load_series_by_ticker(conn)
    conn.close()
    expected = {}
    for ym in months():
        study.SURGE_START, study.SURGE_END = f"{ym}-01", f"{ym}-31"
        for ticker, series in study_series.items():
            for si in study.find_surge_indices(series):
                di = study.find_drop_day(series, si)
                if di is None:
                    continue
                ai = study.find_scenario_a(series, si, di)
                if ai is not None:
                    expected[(ticker, series[si]["date"])] = series[ai]["date"]

    with get_connection() as conn:
        by_ticker = ss.load_series(conn)
    cfg = {"MAX_DAYS_TO_DROP": None, "STALE_AFTER_TRIGGER_DAYS": None, "ENTRY_RULE": "touch"}
    eng = {}
    for ym in months():
        for ev in ss.replay(by_ticker, f"{ym}-01", f"{ym}-31", cfg):
            if ev["status"] == "triggered":
                eng[(ev["ticker"], ev["surge_date"])] = ev["trigger_date"]

    only_study = sorted(set(expected) - set(eng))
    only_engine = sorted(set(eng) - set(expected))
    date_mismatch = sorted(k for k in set(expected) & set(eng) if expected[k] != eng[k])
    print(f"study (touch) entries: {len(expected)}   engine (touch) triggers: {len(eng)}")
    print(f"  only in study: {len(only_study)}  only in engine: {len(only_engine)}  entry-date mismatches: {len(date_mismatch)}")
    for k in (only_study + only_engine + date_mismatch)[:10]:
        print("  diff:", k)
    print("  PARITY OK" if not (only_study or only_engine or date_mismatch) else "  PARITY FAILED")
    sys.exit(0 if not (only_study or only_engine or date_mismatch) else 1)


if __name__ == "__main__":
    main()
