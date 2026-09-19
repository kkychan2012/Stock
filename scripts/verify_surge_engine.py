"""
Parity check: surge_strategy.replay() vs the backtest study's Scenario A trades
(strategies/study_volume_surge_YYYY_MM.json).

1. MAX_DAYS_TO_DROP=None must reproduce the study's Scenario A entries exactly
   (same ticker + surge_date + entry_date).
2. MAX_DAYS_TO_DROP=10 (the live setting) — reports which study trades the cap
   removes and how they performed, so the cap's cost is visible.

Run from the project root:  python scripts/verify_surge_engine.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_setup import get_connection
import surge_strategy as ss

MONTHS = ["2024_11", "2024_12"] + [f"2025_{m:02d}" for m in range(1, 13)] + [f"2026_{m:02d}" for m in range(1, 9)]


def month_range(ym):
    y, m = ym.split("_")
    return f"{y}-{m}-01", f"{y}-{m}-31"


def main():
    study = {}
    for ym in MONTHS:
        with open(f"strategies/study_volume_surge_{ym}.json") as f:
            for t in json.load(f)["trades"]:
                if t["scenario"] == "A":
                    study[(t["ticker"], t["surge_date"])] = t

    with get_connection() as conn:
        by_ticker = ss.load_series(conn)

    def engine_triggers(cap):
        out = {}
        for ym in MONTHS:
            lo, hi = month_range(ym)
            # STALE=None: the live "expire after 3 days" rule doesn't apply to a backtest replay
            for ev in ss.replay(by_ticker, lo, hi, {"MAX_DAYS_TO_DROP": cap, "STALE_AFTER_TRIGGER_DAYS": None}):
                if ev["status"] == "triggered":
                    out[(ev["ticker"], ev["surge_date"])] = ev["trigger_date"]
        return out

    # 1. exact parity, no cap
    eng = engine_triggers(None)
    only_study = sorted(set(study) - set(eng))
    only_engine = sorted(set(eng) - set(study))
    date_mismatch = [k for k in set(study) & set(eng) if study[k]["entry_date"] != eng[k]]
    print(f"[no cap] study Scenario A trades: {len(study)}   engine triggers: {len(eng)}")
    print(f"         only in study: {len(only_study)}  only in engine: {len(only_engine)}  "
          f"entry-date mismatches: {len(date_mismatch)}")
    for k in (only_study + only_engine + date_mismatch)[:10]:
        print("         diff:", k)
    print("         PARITY OK" if not (only_study or only_engine or date_mismatch) else "         PARITY FAILED")

    # 2. effect of the 10-day surge->drop cap
    capped = engine_triggers(10)
    removed = sorted(set(eng) - set(capped))
    print(f"\n[cap=10] engine triggers: {len(capped)}   removed by cap: {len(removed)}")
    if removed:
        rets = [study[k]["blended_return_pct"] for k in removed if k in study]
        allr = [t["blended_return_pct"] for t in study.values()]
        print(f"         removed trades avg return: {sum(rets)/len(rets):.2f}%   "
              f"(all trades avg: {sum(allr)/len(allr):.2f}%)")
        for k in removed[:15]:
            print(f"         removed {k[0]:6s} surge {k[1]}  ret {study[k]['blended_return_pct']}%")


if __name__ == "__main__":
    main()
