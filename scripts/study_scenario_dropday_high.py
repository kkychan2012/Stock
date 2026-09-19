"""
Variant of Scenario A: instead of a limit-buy at the SURGE-DAY CLOSE, this
uses a limit-buy at the DROP DAY's HIGH price.

Rule: after the Drop Day (never bought on the Drop Day itself), wait for
the first day within the next 5 trading days whose intraday High reaches
the Drop Day's own High. Entry price = that Drop Day High (fixed), not the
trigger day's own price.

Same exit as the main study (15% partial target off the surge-day close,
MA21 close-below + next-day-open-confirm cut-loss). Reuses
study_volume_surge_2026_04.py's data loading, surge/drop detection, and
exit simulation so the only thing that differs is the entry trigger price
and level.

Standalone: does not modify or overwrite the existing
study_volume_surge_2026_XX.json/md files. Writes its own comparison report.
"""
import sqlite3
import json
from pathlib import Path

from study_volume_surge_2026_04 import (
    DB_PATH, load_series_by_ticker, add_ma21, find_surge_indices,
    find_drop_day, simulate_exit, blended_return, PARTIAL_TARGET_PCT,
    SCENARIO_A_MAX_DAYS,
)

MONTHS = [
    ("2026-01-01", "2026-01-31", "Jan"),
    ("2026-02-01", "2026-02-28", "Feb"),
    ("2026-03-01", "2026-03-31", "Mar"),
    ("2026-04-01", "2026-04-28", "Apr"),
    ("2026-05-01", "2026-05-31", "May"),
    ("2026-06-01", "2026-06-30", "Jun"),
    ("2026-07-01", "2026-07-31", "Jul"),
    ("2026-08-01", "2026-08-31", "Aug"),
]

OUT_MD = Path("strategies/study_scenario_dropday_high_comparison.md")


def find_entry_dropday_price(series, drop_idx, price_field):
    """Limit-buy at the Drop Day's own price (High or Close, per price_field).
    Trigger = first day after the Drop Day, within the next
    SCENARIO_A_MAX_DAYS trading days, whose intraday High reaches that
    level."""
    trigger_price = series[drop_idx][price_field]
    if trigger_price is None:
        return None, None
    window_start = drop_idx + 1
    window_end = min(window_start + SCENARIO_A_MAX_DAYS, len(series))
    for i in range(window_start, window_end):
        h = series[i]["high"]
        if h is not None and h >= trigger_price:
            return i, trigger_price
    return None, None


def build_trade(ticker, surge_date, surge_close, drop_date, series, trigger_idx, entry_price):
    result = simulate_exit(series, trigger_idx, surge_close, partial_target_pct=PARTIAL_TARGET_PCT)
    ret = blended_return(entry_price, result)
    return {
        "ticker": ticker, "surge_date": surge_date, "drop_date": drop_date,
        "entry_date": series[trigger_idx]["date"], "entry_price": round(entry_price, 4),
        "status": result["status"],
        "exit_date": result.get("exit_date"), "exit_price": result.get("exit_price"),
        "exit_type": result.get("exit_type"),
        "last_date": result.get("last_date"), "last_close": result.get("last_close"),
        "blended_return_pct": round(ret * 100, 2),
    }


def run_month(from_date, to_date, by_ticker, price_field):
    trades = []
    # override module globals for the window, then reuse find_surge_indices
    import study_volume_surge_2026_04 as m
    m.SURGE_START, m.SURGE_END = from_date, to_date
    for ticker, series in by_ticker.items():
        for surge_idx in find_surge_indices(series):
            surge_date = series[surge_idx]["date"]
            surge_close = series[surge_idx]["close"]
            drop_idx = find_drop_day(series, surge_idx)
            if drop_idx is None:
                continue
            drop_date = series[drop_idx]["date"]
            trigger_idx, entry_price = find_entry_dropday_price(series, drop_idx, price_field)
            if trigger_idx is not None:
                trades.append(build_trade(ticker, surge_date, surge_close, drop_date, series, trigger_idx, entry_price))
    return trades


def _stats(trades):
    n = len(trades)
    if n == 0:
        return 0, 0.0, 0.0
    returns = [t["blended_return_pct"] for t in trades]
    avg = sum(returns) / n
    win = 100 * sum(1 for r in returns if r > 0) / n
    return n, avg, win


def main():
    conn = sqlite3.connect(DB_PATH)
    by_ticker = load_series_by_ticker(conn)
    conn.close()
    for series in by_ticker.values():
        add_ma21(series)

    variants = {"dropday_high": {}, "dropday_close": {}}
    for from_date, to_date, label in MONTHS:
        for variant_name, field in [("dropday_high", "high"), ("dropday_close", "close")]:
            trades = run_month(from_date, to_date, by_ticker, field)
            variants[variant_name][label] = trades
        n, avg, win = _stats(variants["dropday_close"][label])
        print(f"{label} (drop-day CLOSE trigger): n={n}  avg={avg:.2f}%  win%={win:.1f}%")

    existing = {}
    for _f, _t, label in MONTHS:
        month_num = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
                     "May": "05", "Jun": "06", "Jul": "07", "Aug": "08"}[label]
        d = json.load(open(f"strategies/study_volume_surge_2026_{month_num}.json"))
        existing[label] = [t for t in d["trades"] if t["scenario"] == "A"]

    write_comparison(existing, variants["dropday_high"], variants["dropday_close"])
    print(f"wrote {OUT_MD}")


def write_comparison(surge_close_trades, dropday_high_trades, dropday_close_trades):
    lines = ["# Entry variant comparison: 3 trigger prices for Scenario A\n"]
    lines.append(
        "All three use the identical exit (15% partial target off surge close, MA21 "
        "close-below + next-day-open-confirm cut-loss) and the same Drop-Day-exclusion rule "
        "(never bought on the Drop Day itself). Only the entry trigger price differs:\n"
        "- **Surge-Close** (current Scenario A): limit-buy at the surge-day close.\n"
        "- **Drop-Day-High**: limit-buy at the Drop Day's own intraday High.\n"
        "- **Drop-Day-Close**: limit-buy at the Drop Day's own Close.\n"
        "In all three, the trigger fires the first day (within 5 trading days after the Drop "
        "Day) whose intraday High reaches the relevant fixed price.\n"
    )
    lines.append(
        "| Month | Surge-Close n/avg/win | Drop-Day-High n/avg/win | Drop-Day-Close n/avg/win |"
    )
    lines.append("|---|---|---|---|")

    all_sc, all_dh, all_dc = [], [], []
    for _f, _t, label in MONTHS:
        sc = surge_close_trades[label]
        dh = dropday_high_trades[label]
        dc = dropday_close_trades[label]
        all_sc.extend(sc); all_dh.extend(dh); all_dc.extend(dc)
        sn, sa, sw = _stats(sc)
        hn, ha, hw = _stats(dh)
        cn, ca, cw = _stats(dc)
        lines.append(
            f"| {label} | {sn} / {sa:.2f}% / {sw:.1f}% | {hn} / {ha:.2f}% / {hw:.1f}% | "
            f"{cn} / {ca:.2f}% / {cw:.1f}% |"
        )

    sn, sa, sw = _stats(all_sc)
    hn, ha, hw = _stats(all_dh)
    cn, ca, cw = _stats(all_dc)
    lines.append(
        f"| **Combined** | **{sn} / {sa:.2f}% / {sw:.1f}%** | **{hn} / {ha:.2f}% / {hw:.1f}%** | "
        f"**{cn} / {ca:.2f}% / {cw:.1f}%** |"
    )

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
