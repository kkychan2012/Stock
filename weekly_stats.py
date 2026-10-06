"""
weekly_stats.py — weekly up/down counter with volume context.

For one ticker over a date range, pulls weekly OHLCV bars from Yahoo Finance
and classifies every week by its candle body:
    up   — Close >  Open  (green candle)
    down — Close <  Open  (red candle)
    flat — Close == Open  (counted in the total, but neither up nor down)

It then compares volume on up weeks vs down weeks (average + total) and finds
the single highest-volume week — a quick read on whether a move was
"confirmed" by volume (up weeks trading heavier than down weeks = healthier).

Two entry points:
    summarize_weeks(weeks)                     pure — list of week dicts in, stats out
    weekly_up_down(ticker, start_date, end_date)  fetches from Yahoo, then summarizes

CLI:  python weekly_stats.py VKTX 2025-04-07 2026-09-30
"""

from datetime import date, datetime, timedelta

# Prices are compared after rounding to this many decimals, so float noise from
# Yahoo's split/dividend adjustment can't turn a genuinely flat week into up/down.
PRICE_DECIMALS = 4


def _parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def classify_week(open_, close):
    o, c = round(open_, PRICE_DECIMALS), round(close, PRICE_DECIMALS)
    if c > o:
        return "up"
    if c < o:
        return "down"
    return "flat"


def summarize_weeks(weeks):
    """weeks: ascending list of dicts with keys date, open, close, volume
    (high/low optional). Adds `direction` and `change_pct` to each dict and
    returns (weeks, summary)."""
    for w in weeks:
        w["direction"] = classify_week(w["open"], w["close"])
        w["change_pct"] = (w["close"] / w["open"] - 1) * 100 if w["open"] else None

    up   = [w for w in weeks if w["direction"] == "up"]
    down = [w for w in weeks if w["direction"] == "down"]
    flat = [w for w in weeks if w["direction"] == "flat"]
    total = len(weeks)

    def _avg(ws):
        return sum(w["volume"] for w in ws) / len(ws) if ws else None

    up_avg, down_avg = _avg(up), _avg(down)
    up_total = sum(w["volume"] for w in up)
    down_total = sum(w["volume"] for w in down)
    top = max(weeks, key=lambda w: w["volume"]) if weeks else None

    summary = {
        "total_weeks": total,
        "up_weeks": len(up),
        "down_weeks": len(down),
        "flat_weeks": len(flat),
        "up_pct": len(up) / total * 100 if total else None,
        "down_pct": len(down) / total * 100 if total else None,
        "avg_volume_up": up_avg,
        "avg_volume_down": down_avg,
        "avg_volume_ratio": up_avg / down_avg if up_avg and down_avg else None,
        "total_volume_up": up_total,
        "total_volume_down": down_total,
        "total_volume_ratio": up_total / down_total if down_total else None,
        "highest_volume_week": (
            {"date": top["date"], "direction": top["direction"], "volume": top["volume"]}
            if top else None
        ),
        "first_close": weeks[0]["close"] if weeks else None,
        "last_close": weeks[-1]["close"] if weeks else None,
    }
    return weeks, summary


def fetch_weekly_bars(ticker, start_date, end_date):
    """Weekly OHLCV bars from Yahoo, ascending. Dates are 'YYYY-MM-DD' strings;
    end_date is inclusive (yfinance's `end` is exclusive, so +1 day is passed).
    Each bar is labelled by the Monday its week starts on."""
    import yfinance as yf

    end_excl = (_parse_date(end_date) + timedelta(days=1)).isoformat()
    df = yf.download(ticker, start=start_date, end=end_excl, interval="1wk",
                     auto_adjust=True, progress=False, multi_level_index=False)
    if df is None or df.empty:
        return []

    weeks = []
    for idx, row in df.iterrows():
        o, c, v = row.get("Open"), row.get("Close"), row.get("Volume")
        if o != o or c != c or o is None or c is None:   # NaN -> skip
            continue
        weeks.append({
            "date": idx.strftime("%Y-%m-%d"),
            "open": float(o),
            "high": float(row.get("High")),
            "low": float(row.get("Low")),
            "close": float(c),
            "volume": int(v) if v == v and v is not None else 0,
        })
    return weeks


def weekly_up_down(ticker, start_date, end_date=None):
    """Fetch + summarize. Returns a JSON-ready dict:
    {ticker, start_date, end_date, summary, weeks, notes}."""
    ticker = ticker.strip().upper()
    end_date = end_date or date.today().isoformat()
    if _parse_date(end_date) < _parse_date(start_date):
        raise ValueError("end_date is before start_date")

    weeks = fetch_weekly_bars(ticker, start_date, end_date)
    weeks, summary = summarize_weeks(weeks)

    # Flag partial weeks at either edge so they can be read with care: a range
    # starting mid-week (Yahoo trims that bar to start_date), or ending before
    # that week's Friday (incl. the current, still-forming week).
    notes = []
    if weeks:
        first_monday = _parse_date(weeks[0]["date"])
        last_friday = _parse_date(weeks[-1]["date"]) + timedelta(days=4)
        if _parse_date(start_date) > first_monday:
            weeks[0]["partial"] = True
            notes.append(f"First week ({weeks[0]['date']}) only covers {start_date} onward — "
                         "Yahoo trims its open and volume to the range.")
        if min(_parse_date(end_date), date.today()) < last_friday:
            weeks[-1]["partial"] = True
            notes.append(f"Last week ({weeks[-1]['date']}) is incomplete — its candle can still change.")

    return {
        "ticker": ticker,
        "start_date": start_date,
        "end_date": end_date,
        "summary": summary,
        "weeks": weeks,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Ranking — top tickers by up/down volume ratio across the whole stored set
# (tracked Ticker List ∪ active S&P 500 + Russell 1000 universe).
#
# Unlike weekly_up_down() this reads the STORED daily bars and rolls them up
# into weeks (open = first day's open, close = last day's close, volume = sum
# — the same definition Yahoo uses for its 1wk bars), so ~1,100 tickers rank
# in about a second with no download. It is only as current as the last Fetch.
# ---------------------------------------------------------------------------
RANK_MONTHS = 4            # default look-back
RANK_TOP_N = 30
RANK_MIN_CHANGE_PCT = 10.0  # minimum price rise, see `basis` in rank_volume_ratio()
RANK_MIN_WEEKS_EACH = 1    # need at least this many up AND down weeks for a ratio


def _months_back(d, months):
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    m += 1
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    last_day = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last_day))


def _monday(d):
    return d - timedelta(days=d.weekday())


def daily_to_weekly(days):
    """days: ascending list of (date_str, open, close, volume) for one ticker.
    Returns ascending week dicts labelled by the week's Monday."""
    weeks = []
    for ds, o, c, v in days:
        monday = _monday(_parse_date(ds)).isoformat()
        if weeks and weeks[-1]["date"] == monday:
            weeks[-1]["close"] = c
            weeks[-1]["volume"] += v or 0
        else:
            weeks.append({"date": monday, "open": o, "close": c, "volume": v or 0})
    return weeks


def rank_volume_ratio(start_date=None, end_date=None, top_n=RANK_TOP_N,
                      min_change_pct=RANK_MIN_CHANGE_PCT, basis="range", conn=None):
    """Top `top_n` tickers by avg-volume ratio (up weeks / down weeks), up side
    only (ratio > 1), among tickers whose price rose more than `min_change_pct`:
        basis="range" — last close vs the close just before the range started
        basis="week"  — the best single week's close vs the previous week's close
    Only whole weeks count: the start is snapped back to its Monday and the
    still-forming current week is left out, since a short week's low volume
    would skew the up/down averages."""
    from db_setup import get_connection

    if basis not in ("range", "week"):
        raise ValueError("basis must be 'range' or 'week'")
    end = _parse_date(end_date) if end_date else date.today()
    start = _parse_date(start_date) if start_date else _months_back(end, RANK_MONTHS)
    if end < start:
        raise ValueError("end_date is before start_date")
    start = _monday(start)
    # a few extra days before the range, for the "previous close" each ticker starts from
    load_from = (start - timedelta(days=10)).isoformat()

    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        # tracked tickers win on overlap — same rule as /api/data/summary
        rows = conn.execute("""
            SELECT s.ticker, s.date, s.open, s.close, s.volume, 1 AS tracked
            FROM stocks_daily s
            WHERE s.date BETWEEN ? AND ?
              AND s.ticker IN (SELECT ticker FROM extraction_tickers)
            UNION ALL
            SELECT u.ticker, u.date, u.open, u.close, u.volume, 0 AS tracked
            FROM universe_prices u
            JOIN ticker_universe t ON t.ticker = u.ticker AND t.is_active = 1
            WHERE u.date BETWEEN ? AND ?
              AND u.ticker NOT IN (SELECT ticker FROM extraction_tickers)
            ORDER BY 1, 2
        """, (load_from, end.isoformat(), load_from, end.isoformat())).fetchall()
    finally:
        if own_conn:
            conn.close()

    by_ticker, tracked, data_through = {}, {}, None
    for tk, ds, o, c, v, is_tracked in rows:
        if o is None or c is None:
            continue
        by_ticker.setdefault(tk, []).append((ds, o, c, v))
        tracked[tk] = bool(is_tracked)
        if data_through is None or ds > data_through:
            data_through = ds

    # the last week is whole only once the data reaches its Friday
    partial_monday = None
    if data_through:
        last = _parse_date(data_through)
        if last.weekday() < 4:
            partial_monday = _monday(last).isoformat()

    start_s = start.isoformat()
    out = []
    for tk, days in by_ticker.items():
        prior = [d for d in days if d[0] < start_s]
        weeks = [w for w in daily_to_weekly([d for d in days if d[0] >= start_s])
                 if w["date"] != partial_monday]
        if not weeks:
            continue
        weeks, s = summarize_weeks(weeks)
        ratio = s["avg_volume_ratio"]
        if (ratio is None or ratio <= 1
                or min(s["up_weeks"], s["down_weeks"]) < RANK_MIN_WEEKS_EACH):
            continue

        base = prior[-1][2] if prior else weeks[0]["open"]
        range_chg = (s["last_close"] / base - 1) * 100 if base else None
        best_week, prev_close = None, base
        for w in weeks:
            if prev_close:
                chg = (w["close"] / prev_close - 1) * 100
                if best_week is None or chg > best_week["change_pct"]:
                    best_week = {"date": w["date"], "change_pct": chg}
            prev_close = w["close"]

        gate = range_chg if basis == "range" else (best_week or {}).get("change_pct")
        if gate is None or gate <= min_change_pct:
            continue
        out.append({
            "ticker": tk,
            "tracked": tracked[tk],
            "avg_volume_ratio": ratio,
            "total_weeks": s["total_weeks"],
            "up_weeks": s["up_weeks"],
            "down_weeks": s["down_weeks"],
            "avg_volume_up": s["avg_volume_up"],
            "avg_volume_down": s["avg_volume_down"],
            "start_price": base,
            "last_close": s["last_close"],
            "range_change_pct": range_chg,
            "best_week": best_week,
            "highest_volume_week": s["highest_volume_week"],
        })

    out.sort(key=lambda r: r["avg_volume_ratio"], reverse=True)
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return {
        "start_date": start_s,
        "end_date": end.isoformat(),
        "data_through": data_through,
        "excluded_partial_week": partial_monday,
        "basis": basis,
        "min_change_pct": min_change_pct,
        "scanned": len(by_ticker),
        "qualified": len(out),
        "rows": out[:top_n],
    }


def _fmt_vol(v):
    if v is None:
        return "—"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"{v / div:.1f}{suf}"
    return str(int(v))


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python weekly_stats.py TICKER START [END]")
        sys.exit(1)
    res = weekly_up_down(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    s = res["summary"]
    print(f"Ticker: {res['ticker']} | Range: {res['start_date']} to {res['end_date']}")
    if not s["total_weeks"]:
        print("No weekly data.")
        sys.exit(0)
    hv = s["highest_volume_week"]
    ratio = f"{s['avg_volume_ratio']:.2f}x" if s["avg_volume_ratio"] else "—"
    rows = [
        ("Total weeks", s["total_weeks"]),
        ("Up weeks", f"{s['up_weeks']} ({s['up_pct']:.0f}%)"),
        ("Down weeks", f"{s['down_weeks']} ({s['down_pct']:.0f}%)"),
        ("Flat weeks", s["flat_weeks"]),
        ("Avg volume (up weeks)", _fmt_vol(s["avg_volume_up"])),
        ("Avg volume (down weeks)", _fmt_vol(s["avg_volume_down"])),
        ("Volume ratio (up/down avg)", ratio),
        ("Total volume (up / down)", f"{_fmt_vol(s['total_volume_up'])} / {_fmt_vol(s['total_volume_down'])}"),
        ("Highest volume week", f"{hv['date']} ({hv['direction'].title()}), {_fmt_vol(hv['volume'])}"),
    ]
    for k, v in rows:
        print(f"  {k:<28} {v}")
    for n in res["notes"]:
        print("  note:", n)
    print()
    for w in res["weeks"]:
        print(f"  {w['date']}  O {w['open']:>9.2f}  C {w['close']:>9.2f}  {w['direction']:<4}  "
              f"{_fmt_vol(w['volume']):>7}{'  (partial)' if w.get('partial') else ''}")
