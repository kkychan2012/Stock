"""
Volume-surge study + capital simulation for Hong Kong stocks — STANDALONE.

Reads hk/hk_stocks.db (built by hk_fetch.py) and reuses the exact same entry /
exit logic as the US study (scripts/study_volume_surge_2026_04.py, Scenario A:
surge day -> red-candle drop day -> limit-buy at the surge close -> +15% partial /
MA21 cut-loss). It only imports pure functions from that script, and never opens
stock_dashboard.db or touches the dashboard.

Universe:  --universe hsi   Hang Seng Index names (hk_tickers.txt)   [default]
           --universe all   every HKEX-listed equity (hk_tickers_all.txt)
Size:      --min-mcap N     ignores surges in stocks whose market cap at the time (close
           the day before the surge x shares outstanding) was below HK$N. Needs
           `python hk/hk_fetch.py --shares` first; stocks with no share count are skipped.
Liquidity: --min-turnover N ignores surges in stocks whose average daily turnover
           (10-day avg volume x close, measured the day BEFORE the surge) was below
           HK$N — thinly traded stocks give unrealistic fills.

Each run writes to its own folder hk/runs/<tag>/ (monthly JSONs in the same format
as the US study, plus the Excel capital simulation with --capital), so runs never
overwrite each other.

Run from the project root:
    python hk/hk_study.py
    python hk/hk_study.py --universe all --min-turnover 5000000 --capital 10000 --position 1000
    python hk/hk_study.py --universe all --vol-mult 3.0 --from 2024-06
"""
import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import study_volume_surge_2026_04 as study          # pure detection/exit functions
import capital_simulation as capsim
import hk_fetch

DB_PATH = hk_fetch.DB_PATH

# Hong Kong per-side trading costs as % of the traded value (buy AND sell), rates in force 2024-26:
#   stamp duty 0.10 + SEHK trading fee 0.00565 + SFC levy 0.0027 + AFRC levy 0.00015 + CCASS 0.002
# Broker commission and slippage are added on top (--commission-pct / --slippage-pct).
HK_GOV_EXCHANGE_PCT = 0.1105


def load_series(tickers=None):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT ticker, date, open, close, low, high, volume, vol_ma10, ma200, ma50 "
        "FROM hk_daily ORDER BY ticker, date").fetchall()
    conn.close()
    keep = set(tickers) if tickers else None
    by_ticker = {}
    for t, d, o, c, lo, hi, v, vm, m200, m50 in rows:
        if keep is not None and t not in keep:
            continue
        by_ticker.setdefault(t, []).append({
            "date": d, "open": o, "close": c, "low": lo, "high": hi,
            "volume": v, "vol_ma10": vm, "ma200": m200, "ma50": m50})
    return by_ticker


def months_between(first, last):
    y, m = int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])
    while (y, m) <= (ly, lm):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            y, m = y + 1, 1


def load_shares():
    conn = sqlite3.connect(DB_PATH)
    try:
        return {t: s for t, s in conn.execute("SELECT ticker, shares FROM hk_securities WHERE shares IS NOT NULL")}
    finally:
        conn.close()


def run_month(by_ticker, ym, min_turnover, min_mcap=0.0, shares=None):
    study.SURGE_START, study.SURGE_END = f"{ym}-01", f"{ym}-31"
    trades, signals, illiquid = [], 0, 0
    for ticker, series in by_ticker.items():
        for surge_idx in study.find_surge_indices(series):
            if min_mcap:
                sh, prev_close = (shares or {}).get(ticker), series[surge_idx - 1]["close"]
                if not sh or not prev_close or sh * prev_close < min_mcap:
                    illiquid += 1                                  # counted in "skipped" (too small / unknown size)
                    continue
            if min_turnover:
                prev = series[surge_idx - 1]                  # liquidity BEFORE the volume spike
                if not prev["vol_ma10"] or not prev["close"] or prev["vol_ma10"] * prev["close"] < min_turnover:
                    illiquid += 1
                    continue
            signals += 1
            drop_idx = study.find_drop_day(series, surge_idx)
            if drop_idx is None:
                continue
            a_idx = study.find_scenario_a(series, surge_idx, drop_idx)
            if a_idx is None:
                continue
            surge_close = series[surge_idx]["close"]
            trades.append(study.build_trade(
                ticker, series[surge_idx]["date"], surge_close, series[drop_idx]["date"],
                "A", series, a_idx, entry_price=surge_close))
    return signals, illiquid, trades


def main():
    ap = argparse.ArgumentParser(description="Hong Kong volume-surge study")
    ap.add_argument("--universe", choices=["hsi", "all"], default="hsi")
    ap.add_argument("--from", dest="start", default="2024-01", help="first surge month YYYY-MM")
    ap.add_argument("--vol-mult", type=float, default=study.VOL_MULT)
    ap.add_argument("--target", type=float, default=study.PARTIAL_TARGET_PCT, help="partial-sell target, e.g. 0.15")
    ap.add_argument("--min-mcap", type=float, default=0.0, help="min market cap in HKD at the time of the surge (0 = no filter)")
    ap.add_argument("--min-turnover", type=float, default=0.0, help="min avg daily turnover in HKD (0 = no filter)")
    ap.add_argument("--capital", type=float, help="also run the capital simulation with this starting capital")
    ap.add_argument("--position", type=float, default=1000.0, help="position size for the capital simulation")
    ap.add_argument("--slots", type=int, help="compounding mode: max positions (profits spread over free slots)")
    ap.add_argument("--max-pos-pct", type=float, help="per-position limit as a fraction of account equity, e.g. 0.10")
    ap.add_argument("--costs", action="store_true", help="charge HK trading costs on every buy and sell")
    ap.add_argument("--commission-pct", type=float, default=0.05, help="broker commission per side, %% (with --costs)")
    ap.add_argument("--slippage-pct", type=float, default=0.0, help="assumed slippage per side, %% (with --costs)")
    ap.add_argument("--max-positions", type=int, help="HARD cap on open positions in the capital simulation (profits not reinvested beyond it)")
    ap.add_argument("--tag", help="output folder name under hk/runs/ (default: built from the options)")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        sys.exit("hk/hk_stocks.db not found - run: python hk/hk_fetch.py")
    study.VOL_MULT = args.vol_mult
    study.PARTIAL_TARGET_PCT = args.target

    tickers = hk_fetch.read_ticker_file(hk_fetch.ALL_FILE if args.universe == "all" else hk_fetch.HSI_FILE)
    by_ticker = load_series(tickers)
    for series in by_ticker.values():
        study.add_ma21(series)
    last_date = max(s[-1]["date"] for s in by_ticker.values())
    tag = args.tag or (f"{args.universe}_vm{args.vol_mult:g}_liq{int(args.min_turnover / 1e6) if args.min_turnover else 0}m"
                       + (f"_cap{int(args.min_mcap / 1e6)}m" if args.min_mcap else ""))
    shares = load_shares() if args.min_mcap else None
    if args.min_mcap:
        have = sum(1 for t in by_ticker if t in shares)
        print(f"market-cap filter HK${args.min_mcap:,.0f}: share counts known for {have}/{len(by_ticker)} tickers")
    run_dir = os.path.join(HERE, "runs", tag)
    out_dir = os.path.join(run_dir, "strategies")
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        if f.startswith("study_volume_surge_") and f.endswith(".json"):
            os.remove(os.path.join(out_dir, f))
    print(f"HK {args.universe}: {len(by_ticker)}/{len(tickers)} tickers with data, through {last_date}, "
          f"VOL_MULT={args.vol_mult:g}, target {args.target:.0%}, min turnover HK${args.min_turnover:,.0f}, "
          f"min mcap HK${args.min_mcap:,.0f}")
    print(f"output: {run_dir}")

    all_trades, tot_signals, tot_illiquid = [], 0, 0
    print(f"\n{'Month':8s} {'signals':>7s} {'skipped':>7s} {'trades':>6s} {'avg ret %':>10s} {'win %':>7s}")
    for ym in months_between(args.start, last_date[:7]):
        signals, illiquid, trades = run_month(by_ticker, ym, args.min_turnover, args.min_mcap, shares)
        tot_signals += signals
        tot_illiquid += illiquid
        with open(os.path.join(out_dir, f"study_volume_surge_{ym.replace('-', '_')}.json"), "w") as f:
            json.dump({"summary": {"month": ym, "signal_count": signals, "illiquid_skipped": illiquid,
                                   "trade_count": len(trades)}, "trades": trades}, f, indent=2)
        rets = [t["blended_return_pct"] for t in trades]
        all_trades += trades
        avg = f"{sum(rets) / len(rets):10.2f}" if rets else f"{'-':>10s}"
        win = f"{100 * sum(r > 0 for r in rets) / len(rets):7.1f}" if rets else f"{'-':>7s}"
        print(f"{ym:8s} {signals:7d} {illiquid:7d} {len(trades):6d} {avg} {win}")

    if all_trades:
        rets = sorted(t["blended_return_pct"] for t in all_trades)
        n = len(rets)
        print(f"\nTOTAL: {tot_signals} surge signals ({tot_illiquid} skipped by the size/liquidity filters) -> {n} Scenario A trades | "
              f"avg {sum(rets) / n:.2f}% | median {rets[n // 2]:.2f}% | win rate {100 * sum(r > 0 for r in rets) / n:.1f}% | "
              f"best {rets[-1]:.1f}% | worst {rets[0]:.1f}%")
    else:
        print("\nNo trades found.")

    if args.capital and all_trades:
        os.chdir(run_dir)                               # capital_simulation reads ./strategies/
        out = f"strategies/capital_simulation_hk_{int(args.capital)}" + (f"_hardcap{args.max_positions}" if args.max_positions else "") \
              + (f"_compound{args.slots}" if args.slots else "") \
              + (f"_mpp{int(round(args.max_pos_pct * 100))}" if args.max_pos_pct else "") \
              + ("_costs" if args.costs else "") \
              + (f"_slip{int(round(args.slippage_pct * 100))}" if args.costs and args.slippage_pct else "") + ".xlsx"
        cost = (HK_GOV_EXCHANGE_PCT + args.commission_pct + args.slippage_pct) / 100 if args.costs else 0.0
        if args.costs:
            print(f"trading costs: {cost * 100:.4f}% per side ({2 * cost * 100:.3f}% round trip)")
        capsim.run_simulation(args.capital, args.position, out, compound_slots=args.slots,
                              max_positions=args.max_positions, max_pos_pct=args.max_pos_pct, cost_pct_side=cost)


if __name__ == "__main__":
    main()
