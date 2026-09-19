"""
Standalone study: volume-surge entry (Scenario A / Scenario B) + a new
two-stage split-position exit, restricted to surge signals in 2026-04-01..2026-04-28.

This is NOT part of the strategy-miner/reviewer/backtester/developer loop.
It does not read or write strategy_log.json, criteria_config.json, or
strategy_harness.py. Results are written to:
  strategies/study_volume_surge_2026_04.json
  strategies/study_volume_surge_2026_04.md

Entry (as specified by the user, not identical to scan_patterns.py's live
_volume_surge() function -- see README note in the output report):
  Surge day:  volume > VOL_MULT * vol_ma10 (currently 3.5)  AND  close > ma200
              AND  ma50 > ma200  AND  close > previous day's close (an up day)
              (tested and rejected: excluding surge days that gap up >12% at
              the open -- it removed ~30% of signals and disproportionately
              cut the biggest winners, so no gap-based filter is applied)
  Drop Day:   first SOLID/FILLED red candle (close < open), strictly after the
              surge day. A green candle never counts, even if it closes below
              the previous day's close.
  Scenario A: limit-buy at the surge-day close price. The Drop Day is a
              signal day only -- never bought on. Trigger = first day AFTER
              the Drop Day, within the next 5 trading days, whose intraday
              High reaches that price level. Entry price = the surge-day
              close itself (not the trigger day's own close). If the level
              is never reached within that window, Scenario A does not
              trigger for that event.
  Scenario B: first confirmed touch-and-reclaim of MA21 after the Drop Day --
              a day where low <= ma21 ("touch"), then the next trading day's
              close > touch day's close ("confirm"); if not confirmed, keep
              scanning for the next touch (entry price = confirm day's close)
  A and B are independent; both are logged for every surge+drop-day event.

Exit (new, defined by the user for this study only):
  Stage 1 (partial): baseline = surge-day close. If close >= baseline*(1+PARTIAL_TARGET_PCT)
                      (currently 15%), sell half at that close (once).
  Stage 2 (cut-loss, MA21-based): if a day's CLOSE is below that day's MA21,
                      and the very next trading day's OPEN is still below
                      that day's MA21 (confirmation), the position is cut --
                      sell full position if stage 1 hasn't triggered yet,
                      else sell the remaining half. Fill price = the
                      confirmation day's OPEN. If the next day's open is back
                      above MA21, no cut, and scanning continues day by day.
  Both stages are checked every day from entry onward; the cut-loss check
  (which resolves at the *next* day's open) is evaluated before that day's
  own close is checked against the partial-sell target.
"""
import sqlite3
import json
from pathlib import Path

DB_PATH = "stock_dashboard.db"
SURGE_START = "2026-04-01"
SURGE_END = "2026-04-28"
VOL_MULT = 3.5
PARTIAL_TARGET_PCT = 0.15
MA21_WINDOW = 21
PROJECT_WIN_BAR = 0.20  # same "win" bar used elsewhere in this project, reported for reference

OUT_JSON = Path("strategies/study_volume_surge_2026_04.json")
OUT_MD = Path("strategies/study_volume_surge_2026_04.md")


def load_series_by_ticker(conn):
    """Tracked tickers (stocks_daily) unioned with the rest of the active
    S&P 500 + Russell 1000 universe (universe_prices); tracked wins on
    overlap -- same merge rule as scan_patterns._load_prices_up_to() and
    /api/data/summary."""
    cur = conn.cursor()
    cols = "ticker, date, open, close, low, high, volume, vol_ma10, ma200, ma50"
    by_ticker = {}

    def _add(rows):
        for ticker, date, open_, close, low, high, volume, vol_ma10, ma200, ma50 in rows:
            by_ticker.setdefault(ticker, []).append({
                "date": date, "open": open_, "close": close, "low": low, "high": high,
                "volume": volume, "vol_ma10": vol_ma10, "ma200": ma200, "ma50": ma50,
            })

    cur.execute(f"SELECT {cols} FROM stocks_daily ORDER BY ticker, date")
    _add(cur.fetchall())
    cur.execute(
        f"SELECT {cols} FROM universe_prices "
        "WHERE ticker IN (SELECT ticker FROM ticker_universe WHERE is_active = 1) "
        "AND ticker NOT IN (SELECT DISTINCT ticker FROM stocks_daily) "
        "ORDER BY ticker, date"
    )
    _add(cur.fetchall())
    return by_ticker


def add_ma21(series):
    closes = [r["close"] for r in series]
    for i in range(len(series)):
        if i >= MA21_WINDOW - 1:
            window = closes[i - MA21_WINDOW + 1 : i + 1]
            series[i]["ma21"] = sum(window) / MA21_WINDOW if all(c is not None for c in window) else None
        else:
            series[i]["ma21"] = None


def find_surge_indices(series):
    idxs = []
    for i, r in enumerate(series):
        if not (SURGE_START <= r["date"] <= SURGE_END):
            continue
        if i == 0:
            continue
        prev_close = series[i - 1]["close"]
        if r["vol_ma10"] is None or r["ma200"] is None or r["ma50"] is None or r["close"] is None or prev_close is None:
            continue
        if (r["volume"] > VOL_MULT * r["vol_ma10"]
                and r["close"] > r["ma200"]
                and r["ma50"] > r["ma200"]
                and r["close"] > prev_close):
            idxs.append(i)
    return idxs


def find_drop_day(series, surge_idx):
    """Drop Day = first day after the surge day with a SOLID/FILLED red candle
    (close < open). A green/hollow candle (close > open) never counts, even
    if its close is below the previous day's close."""
    for i in range(surge_idx + 1, len(series)):
        c, o = series[i]["close"], series[i]["open"]
        if c is not None and o is not None and c < o:
            return i
    return None


SCENARIO_A_MAX_DAYS = 5  # trading days, counted starting the day AFTER the Drop Day


def find_scenario_a(series, surge_idx, drop_idx):
    """Limit-buy at the surge-day close: trigger = first day AFTER the Drop
    Day (never on the Drop Day itself -- it's a signal day only), within the
    next SCENARIO_A_MAX_DAYS trading days, whose intraday High reaches that
    price level. Entry fills at the surge-day close itself, not the
    trigger day's own close."""
    surge_close = series[surge_idx]["close"]
    window_start = drop_idx + 1
    window_end = min(window_start + SCENARIO_A_MAX_DAYS, len(series))
    for i in range(window_start, window_end):
        h = series[i]["high"]
        if h is not None and h >= surge_close:
            return i
    return None


def find_scenario_b(series, drop_idx):
    n = len(series)
    i = drop_idx + 1
    while i < n:
        r = series[i]
        if r["ma21"] is not None and r["low"] is not None and r["low"] <= r["ma21"]:
            confirm_idx = i + 1
            if confirm_idx < n and series[confirm_idx]["close"] > series[i]["close"]:
                return confirm_idx
            i += 1  # touch not confirmed next day -- keep scanning for the next touch
        else:
            i += 1
    return None


def simulate_exit(series, entry_idx, baseline_close, partial_target_pct=PARTIAL_TARGET_PCT):
    """Cut-loss rule: close < MA21 on day i, confirmed if day i+1's OPEN is
    still < MA21 (that day's own MA21) -- sells at that confirmation day's
    open. Partial-sell target (close-based, off the surge-day baseline)
    still runs independently, checked using each day's close."""
    target_price = baseline_close * (1 + partial_target_pct)
    partial = None  # {"date":..., "price":...} once triggered
    n = len(series)

    for i in range(entry_idx + 1, n - 1):
        cur_close = series[i]["close"]
        cur_ma21 = series[i]["ma21"]

        if cur_close is not None and cur_ma21 is not None and cur_close < cur_ma21:
            nxt = series[i + 1]
            if nxt["open"] is not None and nxt["ma21"] is not None and nxt["open"] < nxt["ma21"]:
                return {
                    "status": "closed",
                    "partial": partial,
                    "exit_date": nxt["date"],
                    "exit_price": nxt["open"],
                    "exit_type": "cut_loss_ma21_remaining_half" if partial else "cut_loss_ma21_full",
                    "stop_reference": {
                        "date": series[i]["date"], "price": cur_close,
                        "ma21": round(cur_ma21, 4), "confirm_ma21": round(nxt["ma21"], 4),
                    },
                }

        if cur_close is None:
            continue

        if partial is None and cur_close >= target_price:
            partial = {"date": series[i]["date"], "price": cur_close}

    # last day in the series can still be checked for the partial target
    if n > entry_idx + 1:
        last_i = n - 1
        last_close = series[last_i]["close"]
        if partial is None and last_close is not None and last_close >= target_price:
            partial = {"date": series[last_i]["date"], "price": last_close}

    last = next((r for r in reversed(series) if r["close"] is not None), series[-1])
    return {
        "status": "still_open",
        "partial": partial,
        "last_date": last["date"],
        "last_close": last["close"],
    }


def blended_return(entry_price, result):
    if result["status"] == "closed":
        if result["partial"] is None:
            return (result["exit_price"] - entry_price) / entry_price
        r1 = (result["partial"]["price"] - entry_price) / entry_price
        r2 = (result["exit_price"] - entry_price) / entry_price
        return 0.5 * r1 + 0.5 * r2
    else:
        if result["partial"] is None:
            return (result["last_close"] - entry_price) / entry_price
        r1 = (result["partial"]["price"] - entry_price) / entry_price
        r2 = (result["last_close"] - entry_price) / entry_price
        return 0.5 * r1 + 0.5 * r2


def build_trade(ticker, surge_date, surge_close, drop_date, scenario, series, trigger_idx, entry_price=None):
    if entry_price is None:
        entry_price = series[trigger_idx]["close"]
    result = simulate_exit(series, trigger_idx, surge_close)
    ret = blended_return(entry_price, result)
    return {
        "ticker": ticker,
        "surge_date": surge_date,
        "surge_close": round(surge_close, 4),
        "drop_date": drop_date,
        "scenario": scenario,
        "entry_date": series[trigger_idx]["date"],
        "entry_price": round(entry_price, 4),
        "partial_sell": (
            {"date": result["partial"]["date"], "price": round(result["partial"]["price"], 4)}
            if result.get("partial") else None
        ),
        "status": result["status"],
        "exit_date": result.get("exit_date"),
        "exit_price": round(result["exit_price"], 4) if result.get("exit_price") is not None else None,
        "exit_type": result.get("exit_type"),
        "stop_reference": (
            {
                "date": result["stop_reference"]["date"],
                "price": round(result["stop_reference"]["price"], 4),
                "ma21": result["stop_reference"].get("ma21"),
                "confirm_ma21": result["stop_reference"].get("confirm_ma21"),
            }
            if result.get("stop_reference") else None
        ),
        "last_date": result.get("last_date"),
        "last_close": round(result["last_close"], 4) if result.get("last_close") is not None else None,
        "blended_return_pct": round(ret * 100, 2),
    }


def main():
    conn = sqlite3.connect(DB_PATH)
    by_ticker = load_series_by_ticker(conn)
    conn.close()

    cur_max_date = max(r["date"] for series in by_ticker.values() for r in series)

    surge_events = []
    for ticker, series in by_ticker.items():
        add_ma21(series)
        for surge_idx in find_surge_indices(series):
            surge_events.append((ticker, series, surge_idx))

    signal_count = len(surge_events)

    trades = []
    events_report = []
    for ticker, series, surge_idx in surge_events:
        surge_date = series[surge_idx]["date"]
        surge_close = series[surge_idx]["close"]
        drop_idx = find_drop_day(series, surge_idx)
        if drop_idx is None:
            events_report.append({
                "ticker": ticker, "surge_date": surge_date, "surge_close": round(surge_close, 4),
                "drop_date": None, "note": "no Drop Day found before end of data",
            })
            continue
        drop_date = series[drop_idx]["date"]

        a_idx = find_scenario_a(series, surge_idx, drop_idx)
        b_idx = find_scenario_b(series, drop_idx)

        if a_idx is not None:
            trades.append(build_trade(
                ticker, surge_date, surge_close, drop_date, "A", series, a_idx,
                entry_price=surge_close,
            ))
        if b_idx is not None:
            trades.append(build_trade(ticker, surge_date, surge_close, drop_date, "B", series, b_idx))

        events_report.append({
            "ticker": ticker, "surge_date": surge_date, "surge_close": round(surge_close, 4),
            "drop_date": drop_date,
            "scenario_a_triggered": a_idx is not None,
            "scenario_b_triggered": b_idx is not None,
        })

    n_partial = sum(1 for t in trades if t["partial_sell"] is not None)
    n_full_stop_before_partial = sum(
        1 for t in trades if t["partial_sell"] is None and t["status"] == "closed"
    )
    n_stop_after_partial = sum(
        1 for t in trades if t["partial_sell"] is not None and t["status"] == "closed"
    )
    n_still_open = sum(1 for t in trades if t["status"] == "still_open")

    returns = [t["blended_return_pct"] for t in trades]
    avg_return = round(sum(returns) / len(returns), 2) if returns else None
    n_win_pos = sum(1 for r in returns if r > 0)
    win_rate_pos = round(100 * n_win_pos / len(returns), 1) if returns else None
    n_win_20 = sum(1 for r in returns if r > PROJECT_WIN_BAR * 100)
    win_rate_20 = round(100 * n_win_20 / len(returns), 1) if returns else None

    summary = {
        "study_window": {"from": SURGE_START, "to": SURGE_END},
        "data_available_through": cur_max_date,
        "signal_count": signal_count,
        "trade_count": len(trades),
        "small_sample_warning": signal_count < 20,
        "n_reached_partial_28pct": n_partial,
        "n_fully_stopped_before_28pct": n_full_stop_before_partial,
        "n_stopped_on_remaining_half_after_partial": n_stop_after_partial,
        "n_still_open_at_end_of_data": n_still_open,
        "avg_blended_return_pct": avg_return,
        "win_rate_pct_positive_return": win_rate_pos,
        "win_rate_pct_over_20pct_bar": win_rate_20,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSON, "w") as f:
        json.dump({"summary": summary, "events": events_report, "trades": trades}, f, indent=2)

    write_markdown(summary, trades)

    print(f"signal_count={signal_count}  trade_count={len(trades)}")
    if signal_count < 20:
        print("SMALL SAMPLE WARNING: fewer than 20 signals in this one-month window -- "
              "treat as a qualitative look, not a statistically meaningful backtest.")
    print(f"wrote {OUT_JSON} and {OUT_MD}")


def write_markdown(summary, trades):
    lines = []
    lines.append("# Volume Surge study -- 2026-04 window (standalone, not part of strategy_log.json)\n")
    lines.append(f"- Study window (surge day): {summary['study_window']['from']} .. {summary['study_window']['to']}")
    lines.append(f"- Data available through: {summary['data_available_through']}")
    lines.append(f"- **Surge signals found: {summary['signal_count']}**")
    if summary["small_sample_warning"]:
        lines.append(
            "\n> ⚠️ **Small sample warning**: fewer than 20 surge signals were found in this "
            "one-month window. Treat everything below as a qualitative look at how this exit "
            "strategy behaves, not a statistically meaningful backtest.\n"
        )
    lines.append(f"- Trades logged (Scenario A + Scenario B combined): {summary['trade_count']}")
    lines.append(f"- Reached the {PARTIAL_TARGET_PCT*100:.0f}% partial-sell trigger: {summary['n_reached_partial_28pct']}")
    lines.append(f"- Fully cut before ever reaching {PARTIAL_TARGET_PCT*100:.0f}%: {summary['n_fully_stopped_before_28pct']}")
    lines.append(f"- Stopped on remaining half after a partial sell: {summary['n_stopped_on_remaining_half_after_partial']}")
    lines.append(f"- Still open at end of available data: {summary['n_still_open_at_end_of_data']}")
    lines.append(f"- Average blended return: {summary['avg_blended_return_pct']}%")
    lines.append(f"- Win rate (return > 0%): {summary['win_rate_pct_positive_return']}%")
    lines.append(f"- Win rate (return > 20%, project-standard bar): {summary['win_rate_pct_over_20pct_bar']}%\n")

    lines.append("## Per-trade detail\n")
    lines.append(
        "| Ticker | Surge date | Scenario | Entry date | Entry px | Partial 24% | "
        "MA21 close-below trigger (ref only) | Cut-loss exit | Status | Blended return |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for t in trades:
        partial = (
            f"{t['partial_sell']['date']} @ {t['partial_sell']['price']}"
            if t["partial_sell"] else "never"
        )
        stop_ref = (
            f"{t['stop_reference']['date']} close {t['stop_reference']['price']} < MA21 {t['stop_reference']['ma21']}"
            if t.get("stop_reference") else "n/a (still open)"
        )
        if t["status"] == "closed":
            exit_str = f"{t['exit_date']} @ {t['exit_price']} ({t['exit_type']})"
            status = "closed"
        else:
            exit_str = f"(open) last {t['last_date']} @ {t['last_close']}"
            status = "still open"
        lines.append(
            f"| {t['ticker']} | {t['surge_date']} | {t['scenario']} | {t['entry_date']} | "
            f"{t['entry_price']} | {partial} | {stop_ref} | {exit_str} | {status} | {t['blended_return_pct']}% |"
        )

    lines.append("\n## Assumptions made (not fully specified in the request)\n")
    lines.append(
        "- Surge day filter is exactly the 4 conditions given (`volume > 2.48*vol_ma10`, "
        "`close > ma200`, `ma50 > ma200`, `close > previous day's close`) -- this differs from "
        "`scan_patterns.py`'s live `_volume_surge()`, which also requires a break above prior "
        "High_30D and does not check MA200/MA50 at all. No dedup/cooldown was applied across "
        "consecutive surge days on the same ticker -- each qualifying day is logged as its own "
        "signal."
    )
    lines.append(
        "- `ma21` is not a stored column in `stocks_daily`; it was computed here as a simple "
        "21-trading-day rolling mean of `close`."
    )
    lines.append(
        "- Scenario B retry behavior: if a touch day's confirmation fails (next close is not "
        "above the touch day's close), the search keeps scanning forward for the next touch, "
        "rather than giving up after one attempt."
    )
    lines.append(
        "- Scenario A is a limit-buy at the surge-day close price. The Drop Day is a signal day "
        "only -- never bought on. Trigger = first day *after* the Drop Day, within the next 5 "
        "trading days, whose intraday **High** reaches that price level; entry price = the "
        "surge-day close itself, not the trigger day's own close. If the level is never reached "
        "in that window, Scenario A does not trigger for that event (no trade logged)."
    )
    lines.append(
        "- **Cut-loss replaced**: the old rolling-10-day-high trailing stop is gone. The new rule "
        "is: a day's CLOSE is below that day's MA21, and the very next trading day's OPEN is "
        "still below that (next) day's MA21 -- that confirms the cut, and the fill price is the "
        "confirmation day's OPEN. If the next day's open recovers back above MA21, no cut, and "
        "scanning continues day by day looking for the next close-below-MA21 setup."
    )
    lines.append(
        "- The MA21 close-below-trigger column is reference only (shows which day set up the "
        "cut, and its close/MA21 values) -- no trade happens on that date; the actual sell "
        "happens the next day, at that day's open."
    )
    lines.append(
        "- The partial-sell target (24%) still uses daily **close** off the surge-day baseline, "
        "checked every day the cut-loss setup didn't fire that day."
    )
    lines.append(
        "- \"Win\" is reported two ways: simple positive blended return, and the >20% bar used "
        "elsewhere in this project's strategy loop -- neither was specified for this study."
    )

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        SURGE_START = sys.argv[1]
        SURGE_END = sys.argv[2]
        suffix = sys.argv[3] if len(sys.argv) >= 4 else SURGE_START[:7].replace("-", "_")
        OUT_JSON = Path(f"strategies/study_volume_surge_{suffix}.json")
        OUT_MD = Path(f"strategies/study_volume_surge_{suffix}.md")
    main()
