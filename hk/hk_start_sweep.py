"""
Start-date sensitivity for the Hong Kong capital simulation — STANDALONE.

Re-runs the capital simulation once per possible START MONTH (only signals from that
month on), so you can see how much the P/L depends on when you would have begun.
Uses an existing study run (hk/runs/<tag>/strategies/) and the "base stake + distributed
profit, hard slot cap" sizing: every buy is `position` plus an even share of the profit
made so far (floored at `position`), never more than `slots` positions open.

Run from the project root (after hk_study.py has produced the run):
    python hk/hk_start_sweep.py
    python hk/hk_start_sweep.py --tag all_vm3.5_liq5m_cap1000m --position 10000 --slots 20 10
"""
import argparse
import contextlib
import io
import os
import sys
import tempfile

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import capital_simulation as capsim


def main():
    ap = argparse.ArgumentParser(description="HK start-month sweep")
    ap.add_argument("--tag", default="all_vm3.5_liq5m_cap1000m", help="run folder under hk/runs/")
    ap.add_argument("--position", type=float, default=10000.0, help="base stake per position (HKD)")
    ap.add_argument("--slots", type=int, nargs="+", default=[20, 10], help="max open positions; capital = slots x position")
    ap.add_argument("--from", dest="start", default="2024-01", help="first start month YYYY-MM")
    ap.add_argument("--window", type=int, help="only count signals for this many months after each start (equal-length windows)")
    args = ap.parse_args()

    run_dir = os.path.join(HERE, "runs", args.tag)
    os.chdir(run_dir)                                    # capital_simulation reads ./strategies/
    months = capsim._find_months()
    first = args.start.replace("-", "_")
    months = [m for m in months if m >= first]

    def add_months(ym, n):
        y, mo = int(ym[:4]), int(ym[5:7]) + n
        y += (mo - 1) // 12
        return f"{y:04d}-{(mo - 1) % 12 + 1:02d}"

    last_month = months[-1].replace("_", "-")
    rows = []
    for slots in args.slots:
        capital = slots * args.position
        for m in months:
            ym = m.replace("_", "-")
            end = None
            if args.window:
                end_ym = add_months(ym, args.window - 1)
                if end_ym > last_month:                  # window would run past the data
                    continue
                end = f"{end_ym}-31"
            tmp = os.path.join(tempfile.gettempdir(), f"hk_start_{slots}_{m}.xlsx")
            with contextlib.redirect_stdout(io.StringIO()):
                res = capsim.run_simulation(capital, args.position, tmp, compound_slots=slots,
                                          start_date=f"{ym}-01", end_date=end)
            os.remove(tmp)
            t, s = res["trades"], res["summary"]
            if t.empty:
                continue
            t = t.assign(m=t.buy_date.astype(str).str[:7])
            mp = t.groupby("m").total_pnl.sum()
            cum = mp.cumsum()
            rows.append({
                "slots": slots, "start": ym, "months": len(mp), "executed": int(s["trades_executed"]),
                "skipped": int(s["trades_skipped"]), "pnl": round(s["total_pnl"]),
                "return_pct": round(s["total_return_pct"], 1), "ending_value": round(s["total_ending_value"]),
                "win_pct": round(100 * (t.total_pnl > 0).mean(), 1), "avg_trade_pct": round(t.total_pnl_pct.mean(), 2),
                "worst_month": round(mp.min()), "max_drawdown": round((cum - cum.cummax()).min()),
            })
    df = pd.DataFrame(rows)
    out = os.path.join(run_dir, f"start_sweep{'_w' + str(args.window) if args.window else ''}.csv")
    df.to_csv(out, index=False)
    print(df.to_string(index=False))
    print(f"\nsaved {out}")


if __name__ == "__main__":
    main()
