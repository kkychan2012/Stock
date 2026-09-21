"""
US volume-surge backtest under different BUY-FILL assumptions, side by side.

Scenario A buys at the surge-day close after a red-candle drop day. The original backtest fills exactly at
that level whenever the day's High reaches it - even on days the stock never traded down to it. This runs
the same signals with realistic fills (see ENTRY_MODE in study_volume_surge_2026_04.py):

    optimistic  original: fill at the level whenever High >= level
    stop        buy on the way up: pay max(open, level)
    limit       true buy limit: only when Low <= level, pay min(open, level)

Each (volume multiple, mode) gets its own folder us_entry_runs/vm<X>/<mode>/strategies/ (monthly JSONs in the
study format) and an Excel capital simulation (compounding, 20 slots, "base + shared profit" sizing).

Run from the project root:
    python scripts/study_entry_modes.py
    python scripts/study_entry_modes.py --data-through 2026-09-18 --capital 20000 --position 1000 --slots 20
    python scripts/study_entry_modes.py --modes stop limit --vol-mults 2 2.5 3 3.5 4 5 6
"""
import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import study_volume_surge_2026_04 as study
import capital_simulation as capsim

OUT_ROOT = "us_entry_runs"


def months(first, last):
    y, m = int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])
    while (y, m) <= (ly, lm):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            y, m = y + 1, 1


def run_mode(mode, vm, by_ticker, first, last, capital, position, slots):
    study.ENTRY_MODE = mode
    study.VOL_MULT = vm
    out_dir = os.path.join(OUT_ROOT, f"vm{vm:g}", mode, "strategies")
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        if f.startswith("study_volume_surge_") and f.endswith(".json"):
            os.remove(os.path.join(out_dir, f))
    trades_all = []
    for ym in months(first, last):
        study.SURGE_START, study.SURGE_END = f"{ym}-01", f"{ym}-31"
        trades = []
        for ticker, series in by_ticker.items():
            for surge_idx in study.find_surge_indices(series):
                drop_idx = study.find_drop_day(series, surge_idx)
                if drop_idx is None:
                    continue
                a_idx = study.find_scenario_a(series, surge_idx, drop_idx)
                if a_idx is None:
                    continue
                surge_close = series[surge_idx]["close"]
                trades.append(study.build_trade(
                    ticker, series[surge_idx]["date"], surge_close, series[drop_idx]["date"], "A", series, a_idx,
                    entry_price=study.entry_fill(series, a_idx, surge_close)))
        with open(os.path.join(out_dir, f"study_volume_surge_{ym.replace('-', '_')}.json"), "w") as f:
            json.dump({"trades": trades}, f, indent=2)
        trades_all += trades
    cwd = os.getcwd()
    os.chdir(os.path.join(OUT_ROOT, f"vm{vm:g}", mode))        # capital_simulation reads ./strategies/
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            res = capsim.run_simulation(capital, position, "strategies/capital_simulation_us.xlsx", compound_slots=slots)
    finally:
        os.chdir(cwd)
    return trades_all, res["summary"]


def main():
    ap = argparse.ArgumentParser(description="US backtest under different buy-fill assumptions")
    ap.add_argument("--modes", nargs="+", default=["touch", "optimistic", "stop", "limit"])
    ap.add_argument("--vol-mults", nargs="+", type=float, default=[study.VOL_MULT],
                    help="surge volume multiples to test (default: the study's 3.5)")
    ap.add_argument("--from", dest="first", default="2024-11")
    ap.add_argument("--to", dest="last", default="2026-09")
    ap.add_argument("--data-through", default="2026-09-18", help="ignore data after this date (drops an unfinished day)")
    ap.add_argument("--capital", type=float, default=20000.0)
    ap.add_argument("--position", type=float, default=1000.0)
    ap.add_argument("--slots", type=int, default=20)
    args = ap.parse_args()

    conn = sqlite3.connect(study.DB_PATH)
    by_ticker = study.load_series_by_ticker(conn)
    conn.close()
    for t in list(by_ticker):
        by_ticker[t] = [r for r in by_ticker[t] if r["date"] <= args.data_through]
        study.add_ma21(by_ticker[t])
    print(f"US universe: {len(by_ticker)} tickers, data through {args.data_through}, months {args.first}..{args.last}")

    print(f"\n{'vol x':>5s} {'mode':11s} {'trades':>6s} {'avg %':>7s} {'median %':>9s} {'win %':>6s} | {'sim exec':>8s} {'skipped':>7s} {'P/L':>10s} {'return %':>9s}")
    for vm in args.vol_mults:
        for mode in args.modes:
            trades, s = run_mode(mode, vm, by_ticker, args.first, args.last, args.capital, args.position, args.slots)
            r = sorted(t["blended_return_pct"] for t in trades)
            n = len(r)
            if not n:
                print(f"{vm:5g} {mode:11s} {0:6d}")
                continue
            print(f"{vm:5g} {mode:11s} {n:6d} {sum(r) / n:7.2f} {r[n // 2]:9.2f} {100 * sum(x > 0 for x in r) / n:6.1f} | "
                  f"{int(s['trades_executed']):8d} {int(s['trades_skipped']):7d} {s['total_pnl']:10,.0f} {s['total_return_pct']:9.1f}")


if __name__ == "__main__":
    main()
