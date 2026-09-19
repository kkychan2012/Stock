"""
Parameter sweep: what partial-sell target (currently 28%) maximizes results
for the Scenario A trades in the 2026-04 volume-surge study?

Reuses the same entry set (surge/drop/Scenario A trigger detection is
unaffected by the partial-sell target -- only the exit simulation changes),
from study_volume_surge_2026_04.py. Standalone: does not touch
strategy_log.json or the main study's output files.

Output: strategies/study_volume_surge_2026_04_target_sweep.md
"""
import sqlite3
from pathlib import Path

from study_volume_surge_2026_04 import (
    DB_PATH, load_series_by_ticker, add_ma21, find_surge_indices,
    find_drop_day, find_scenario_a, simulate_exit, blended_return,
)

OUT_MD = Path("strategies/study_volume_surge_2026_04_target_sweep.md")
SWEEP_PCTS = [round(x * 0.01, 2) for x in range(5, 61, 1)]  # 5% .. 60%, step 1%


def main():
    conn = sqlite3.connect(DB_PATH)
    by_ticker = load_series_by_ticker(conn)
    conn.close()

    # Build the Scenario A entry set once (entry logic is independent of the
    # partial-sell target).
    entries = []  # (ticker, surge_date, surge_close, series, a_idx)
    for ticker, series in by_ticker.items():
        add_ma21(series)
        for surge_idx in find_surge_indices(series):
            drop_idx = find_drop_day(series, surge_idx)
            if drop_idx is None:
                continue
            a_idx = find_scenario_a(series, surge_idx, drop_idx)
            if a_idx is not None:
                entries.append((ticker, series[surge_idx]["date"], series[surge_idx]["close"], series, a_idx))

    n_entries = len(entries)
    print(f"Scenario A entries (fixed across sweep): {n_entries}")

    rows = []
    for pct in SWEEP_PCTS:
        returns = []
        n_partial = 0
        n_closed_full_before_partial = 0
        n_closed_after_partial = 0
        n_open = 0
        for ticker, surge_date, surge_close, series, a_idx in entries:
            entry_price = surge_close  # Scenario A entry price = surge close (fixed)
            result = simulate_exit(series, a_idx, surge_close, partial_target_pct=pct)
            ret = blended_return(entry_price, result)
            returns.append(ret * 100)
            if result["status"] == "closed":
                if result["partial"] is not None:
                    n_closed_after_partial += 1
                    n_partial += 1
                else:
                    n_closed_full_before_partial += 1
            else:
                n_open += 1
                if result["partial"] is not None:
                    n_partial += 1

        avg_ret = sum(returns) / len(returns)
        win_pos = sum(1 for r in returns if r > 0)
        win_20 = sum(1 for r in returns if r > 20)
        median_ret = sorted(returns)[len(returns) // 2]
        worst = min(returns)
        best = max(returns)

        rows.append({
            "target_pct": round(pct * 100, 0),
            "avg_return": round(avg_ret, 2),
            "median_return": round(median_ret, 2),
            "win_rate_pos": round(100 * win_pos / len(returns), 1),
            "win_rate_20": round(100 * win_20 / len(returns), 1),
            "n_reached_partial": n_partial,
            "n_full_stop_no_partial": n_closed_full_before_partial,
            "n_stopped_after_partial": n_closed_after_partial,
            "n_open": n_open,
            "worst": round(worst, 2),
            "best": round(best, 2),
        })

    best_by_avg = max(rows, key=lambda r: r["avg_return"])
    best_by_win = max(rows, key=lambda r: r["win_rate_pos"])

    print(f"Best avg return: {best_by_avg['target_pct']:.0f}% target -> avg {best_by_avg['avg_return']}%")
    print(f"Best win rate:   {best_by_win['target_pct']:.0f}% target -> win rate {best_by_win['win_rate_pos']}%")

    write_markdown(n_entries, rows, best_by_avg, best_by_win)
    print(f"wrote {OUT_MD}")


def write_markdown(n_entries, rows, best_by_avg, best_by_win):
    lines = []
    lines.append("# Partial-sell target sweep -- Scenario A, 2026-04 study window\n")
    lines.append(
        "Sweeps the 28% partial-sell target (all else fixed: same 31 Scenario A "
        "entries, 8% trailing stop off a rolling 10-day close high) to see how "
        "results respond. Entry dates/prices don't change across the sweep -- "
        "only when/whether the partial sell fires changes.\n"
    )
    lines.append(f"- Scenario A trade count (fixed across all rows): {n_entries}")
    lines.append(
        "\n> ⚠️ This is still the same 31-trade, one-month sample as the base study. "
        "Sweeping a parameter over a small sample finds what happened to work best "
        "*in this specific month*, not a robust optimum -- treat the \"best\" row as "
        "a hint to test on more data, not as a new default.\n"
    )
    lines.append(
        f"- **Best average return: {best_by_avg['target_pct']:.0f}% target "
        f"-> avg {best_by_avg['avg_return']}%** (vs {[r for r in rows if r['target_pct']==28][0]['avg_return']}% at the original 28%)"
    )
    lines.append(
        f"- **Best win rate (>0%): {best_by_win['target_pct']:.0f}% target "
        f"-> {best_by_win['win_rate_pos']}%** (vs {[r for r in rows if r['target_pct']==28][0]['win_rate_pos']}% at the original 28%)\n"
    )

    lines.append("## Full sweep\n")
    lines.append(
        "| Target % | Avg return | Median return | Win rate (>0%) | Win rate (>20%) | "
        "Reached partial | Full-stop, no partial | Stopped after partial | Still open | Worst | Best |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        marker = " **<- orig 28%**" if r["target_pct"] == 28 else ""
        lines.append(
            f"| {r['target_pct']:.0f}%{marker} | {r['avg_return']}% | {r['median_return']}% | "
            f"{r['win_rate_pos']}% | {r['win_rate_20']}% | {r['n_reached_partial']} | "
            f"{r['n_full_stop_no_partial']} | {r['n_stopped_after_partial']} | {r['n_open']} | "
            f"{r['worst']}% | {r['best']}% |"
        )

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
