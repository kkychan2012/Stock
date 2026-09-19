"""
Check the live position evaluator (surge_strategy.evaluate_position) against the
backtest: for every Scenario A trade the study closed on the MA21 cut-loss, replay
the evaluator over the stored bars from the entry day and compare the sell date.

Known, intentional difference: the backtest compares the confirmation day's open to
that day's MA21 (which includes that day's close); the live rule uses MA21 as of the
break day (knowable at the open) — so a small number of mismatches is expected.

Run from the project root:  python scripts/verify_surge_exit.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_setup import get_connection
import surge_strategy as ss

MONTHS = ["2024_11", "2024_12"] + [f"2025_{m:02d}" for m in range(1, 13)] + [f"2026_{m:02d}" for m in range(1, 9)]


def main():
    trades = []
    for ym in MONTHS:
        with open(f"strategies/study_volume_surge_{ym}.json") as f:
            trades += [t for t in json.load(f)["trades"] if t["scenario"] == "A"]

    with get_connection() as conn:
        series = ss.load_series(conn)

    same = diff = no_sell = 0
    target_same = target_diff = 0
    diffs = []
    for t in trades:
        rows = series.get(t["ticker"])
        if not rows:
            continue
        last_date = rows[-1]["date"]
        target = t["surge_close"] * (1 + ss.CONFIG["PARTIAL_TARGET_PCT"])
        ev = ss.evaluate_position(rows, t["entry_date"], target, False, today=last_date, final_today=True)

        # partial target: study's partial_sell date vs evaluator's first close >= target
        study_partial = t["partial_sell"]["date"] if t["partial_sell"] else None
        # (study's partial is only recorded up to the cut-loss day; compare only when set)
        if study_partial:
            target_same += ev["target_hit_date"] == study_partial
            target_diff += ev["target_hit_date"] != study_partial

        if t["status"] != "closed":
            continue
        if ev["alert_state"] != "sell":
            no_sell += 1
            diffs.append((t["ticker"], t["surge_date"], t["exit_date"], "no sell found"))
        elif ev["sell_date"] == t["exit_date"]:
            same += 1
        else:
            diff += 1
            diffs.append((t["ticker"], t["surge_date"], t["exit_date"], ev["sell_date"]))

    closed = same + diff + no_sell
    print(f"closed trades compared: {closed}")
    print(f"  same sell date: {same} ({100 * same / closed:.1f}%)   different date: {diff}   no sell found: {no_sell}")
    print(f"partial-target date: same {target_same}, different {target_diff}")
    for d in diffs[:12]:
        print("  diff (ticker, surge, study exit, live):", d)


if __name__ == "__main__":
    main()
