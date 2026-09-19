"""
2D parameter sweep for the volume-surge Scenario A strategy:
  - VOL_MULT (surge-day volume threshold): [2.48, 2.8, 3.0, 3.25, 3.5]
  - PARTIAL_TARGET_PCT (partial profit-take target): [0.15, 0.18, 0.20, 0.24, 0.28]

Sweeps across the full Nov 2024 - Aug 2026 window (22 months) using the real
DB (stock_dashboard.db). Reuses study_volume_surge_2026_04.py's entry/exit
logic (Scenario A only) by overriding its SURGE_START/SURGE_END/VOL_MULT
module globals in memory -- does not modify that file's actual constants.

For efficiency: entry detection (surge day -> Drop Day -> Scenario A
trigger) is done ONCE per VOL_MULT value across all 22 months (5 passes),
then each PARTIAL_TARGET_PCT value is evaluated by re-running simulate_exit
over the cached entries (5x5 = 25 combos total, only 5 expensive scans).

Outputs:
  strategies/study_param_sweep_vol_target.json
  strategies/study_param_sweep_vol_target.md
"""
import sys
import os
import sqlite3
import json
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import study_volume_surge_2026_04 as m

VOL_MULTS = [2.48, 2.8, 3.0, 3.25, 3.5]
PARTIAL_TARGETS = [0.15, 0.18, 0.20, 0.24, 0.28]

MONTHS = [
    ("2024-11-01", "2024-11-30"), ("2024-12-01", "2024-12-31"),
    ("2025-01-01", "2025-01-31"), ("2025-02-01", "2025-02-28"),
    ("2025-03-01", "2025-03-31"), ("2025-04-01", "2025-04-30"),
    ("2025-05-01", "2025-05-31"), ("2025-06-01", "2025-06-30"),
    ("2025-07-01", "2025-07-31"), ("2025-08-01", "2025-08-31"),
    ("2025-09-01", "2025-09-30"), ("2025-10-01", "2025-10-31"),
    ("2025-11-01", "2025-11-30"), ("2025-12-01", "2025-12-31"),
    ("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-28"),
    ("2026-03-01", "2026-03-31"), ("2026-04-01", "2026-04-30"),
    ("2026-05-01", "2026-05-31"), ("2026-06-01", "2026-06-30"),
    ("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-08-31"),
]

OUT_JSON = Path("strategies/study_param_sweep_vol_target.json")
OUT_MD = Path("strategies/study_param_sweep_vol_target.md")


def load_all_data():
    conn = sqlite3.connect("stock_dashboard.db")
    by_ticker = m.load_series_by_ticker(conn)
    conn.close()
    for series in by_ticker.values():
        m.add_ma21(series)
    return by_ticker


def find_entries_for_vol_mult(by_ticker, vol_mult):
    """Returns list of (surge_close, series, entry_idx) for every triggered
    Scenario A entry across all 22 months, for this VOL_MULT value."""
    m.VOL_MULT = vol_mult
    entries = []
    for start, end in MONTHS:
        m.SURGE_START, m.SURGE_END = start, end
        for ticker, series in by_ticker.items():
            for surge_idx in m.find_surge_indices(series):
                drop_idx = m.find_drop_day(series, surge_idx)
                if drop_idx is None:
                    continue
                a_idx = m.find_scenario_a(series, surge_idx, drop_idx)
                if a_idx is not None:
                    surge_close = series[surge_idx]["close"]
                    entries.append((surge_close, series, a_idx))
    return entries


def evaluate(entries, partial_target_pct):
    returns = []
    for surge_close, series, entry_idx in entries:
        result = m.simulate_exit(series, entry_idx, surge_close, partial_target_pct=partial_target_pct)
        ret = m.blended_return(surge_close, result)
        returns.append(ret * 100)
    n = len(returns)
    if n == 0:
        return {"n_trades": 0, "avg_return": None, "win_rate_pos": None, "win_rate_20": None}
    avg = sum(returns) / n
    win_pos = 100 * sum(1 for r in returns if r > 0) / n
    win_20 = 100 * sum(1 for r in returns if r > 20) / n
    return {"n_trades": n, "avg_return": round(avg, 2), "win_rate_pos": round(win_pos, 1), "win_rate_20": round(win_20, 1)}


def main():
    print("Loading full DB (1143 tickers, all history)...")
    by_ticker = load_all_data()

    grid = {}  # grid[vol_mult][target] = evaluate() dict
    for vm in VOL_MULTS:
        print(f"Scanning entries for VOL_MULT={vm} across 22 months...")
        entries = find_entries_for_vol_mult(by_ticker, vm)
        print(f"  -> {len(entries)} Scenario A entries found")
        grid[vm] = {}
        for pt in PARTIAL_TARGETS:
            grid[vm][pt] = evaluate(entries, pt)

    # Print grids
    print("\n=== Avg return % ===")
    header = "VOL_MULT\\TARGET  " + "  ".join(f"{pt:>7.0%}" for pt in PARTIAL_TARGETS)
    print(header)
    for vm in VOL_MULTS:
        row = f"{vm:>15}  " + "  ".join(f"{grid[vm][pt]['avg_return']:>7}" for pt in PARTIAL_TARGETS)
        print(row)

    print("\n=== Win rate (>0%) ===")
    print(header)
    for vm in VOL_MULTS:
        row = f"{vm:>15}  " + "  ".join(f"{grid[vm][pt]['win_rate_pos']:>7}" for pt in PARTIAL_TARGETS)
        print(row)

    print("\n=== Trade counts ===")
    print(header)
    for vm in VOL_MULTS:
        row = f"{vm:>15}  " + "  ".join(f"{grid[vm][pt]['n_trades']:>7}" for pt in PARTIAL_TARGETS)
        print(row)

    # Find best by avg return and by win rate
    best_avg = max(
        ((vm, pt, grid[vm][pt]) for vm in VOL_MULTS for pt in PARTIAL_TARGETS),
        key=lambda x: x[2]['avg_return'] if x[2]['avg_return'] is not None else -999
    )
    best_win = max(
        ((vm, pt, grid[vm][pt]) for vm in VOL_MULTS for pt in PARTIAL_TARGETS),
        key=lambda x: x[2]['win_rate_pos'] if x[2]['win_rate_pos'] is not None else -999
    )
    print(f"\nBest avg return: VOL_MULT={best_avg[0]}, TARGET={best_avg[1]} -> {best_avg[2]}")
    print(f"Best win rate:   VOL_MULT={best_win[0]}, TARGET={best_win[1]} -> {best_win[2]}")

    # Save JSON
    json_out = {
        "vol_mults": VOL_MULTS, "partial_targets": PARTIAL_TARGETS,
        "grid": {str(vm): {str(pt): grid[vm][pt] for pt in PARTIAL_TARGETS} for vm in VOL_MULTS},
        "best_by_avg_return": {"vol_mult": best_avg[0], "target": best_avg[1], **best_avg[2]},
        "best_by_win_rate": {"vol_mult": best_win[0], "target": best_win[1], **best_win[2]},
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSON, "w") as f:
        json.dump(json_out, f, indent=2)

    write_markdown(grid, best_avg, best_win)
    print(f"\nWrote {OUT_JSON} and {OUT_MD}")


def write_markdown(grid, best_avg, best_win):
    lines = ["# Parameter sweep: VOL_MULT x PARTIAL_TARGET_PCT (Scenario A, Nov 2024 - Aug 2026)\n"]
    lines.append(
        "22-month window, all 1143 tickers, Scenario A only (surge-close limit-buy, "
        "MA21 cut-loss exit). Entry detection re-run per VOL_MULT value; exit "
        "re-simulated per PARTIAL_TARGET_PCT value on the same cached entries.\n"
    )

    lines.append("## Avg blended return %\n")
    lines.append("| VOL_MULT \\ TARGET | " + " | ".join(f"{pt:.0%}" for pt in PARTIAL_TARGETS) + " |")
    lines.append("|---|" + "---|" * len(PARTIAL_TARGETS))
    for vm in VOL_MULTS:
        lines.append(f"| {vm} | " + " | ".join(f"{grid[vm][pt]['avg_return']}%" for pt in PARTIAL_TARGETS) + " |")

    lines.append("\n## Win rate (>0%)\n")
    lines.append("| VOL_MULT \\ TARGET | " + " | ".join(f"{pt:.0%}" for pt in PARTIAL_TARGETS) + " |")
    lines.append("|---|" + "---|" * len(PARTIAL_TARGETS))
    for vm in VOL_MULTS:
        lines.append(f"| {vm} | " + " | ".join(f"{grid[vm][pt]['win_rate_pos']}%" for pt in PARTIAL_TARGETS) + " |")

    lines.append("\n## Trade counts\n")
    lines.append("| VOL_MULT \\ TARGET | " + " | ".join(f"{pt:.0%}" for pt in PARTIAL_TARGETS) + " |")
    lines.append("|---|" + "---|" * len(PARTIAL_TARGETS))
    for vm in VOL_MULTS:
        lines.append(f"| {vm} | " + " | ".join(f"{grid[vm][pt]['n_trades']}" for pt in PARTIAL_TARGETS) + " |")

    lines.append(f"\n## Best by avg return\nVOL_MULT={best_avg[0]}, TARGET={best_avg[1]:.0%} -> {best_avg[2]}\n")
    lines.append(f"## Best by win rate\nVOL_MULT={best_win[0]}, TARGET={best_win[1]:.0%} -> {best_win[2]}\n")

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
