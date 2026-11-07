"""
daily_brief.py — nightly "Morning Brief": 10 stocks worth a look, IB-research style.

Run by the Windows scheduled task "StockDashboard Daily Brief" (Mon-Fri 23:30
UK time, after the US close and after nightly_collect.py's 23:00 insider /
Congress / Form 144 / 13D-G collection). Output: reports/daily/<as_of>.html,
served by the dashboard at /report (latest), /report/<date>, /reports (list).

Pipeline
  1. Waits (up to 90 min) for nightly_collect.py to finish, so tonight's
     insider/Congress data is in.
  2. Quick US price update (fetch_data.quick_update: last 20 days, whole
     universe, RS + Trend Template) -> Surge scan -> Pattern scan for the day.
  3. Scores every liquid US stock (tracked list + S&P 500/Russell 1000):
       Technical  — Trend Template score, RS Rating/leader, 30-day/52-week
                    breakouts, volume vs 10-day average, the 9 pattern
                    scanners (VCP, Cup & Handle, MA200 breakout, ...),
                    Surge-strategy signals; penalties for over-extension.
       Smart money— open-market insider buying (Form 4 code P, last 30 d),
                    Congress/executive buys (filed last 45 d), new 13D stakes
                    (last 30 d); flags for heavy insider selling / Form 144.
  4. Picks 10 (7 best overall + 3 best smart-money names with acceptable
     technicals, falling back to overall), then per pick: Google News (7 d),
     Yahoo fundamentals / analyst target / next earnings, Volume Profile
     support/resistance, and a chart (price + MA21/50/150/200, 52-wk high,
     key levels, insider/Congress/144/13D markers, volume, RS line).
  5. Compares with earlier reports (daily_report_picks table): repeat
     appearances and a scorecard of recent picks vs the S&P 500.

Usage:
  python daily_brief.py                    # what the task runs
  python daily_brief.py --as-of 2026-11-04 --no-update   # rebuild a day from stored data
  python daily_brief.py --force            # overwrite an existing report for the day
"""
import argparse
import base64
import html
import io
import json
import logging
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from db_setup import get_connection, setup_database  # noqa: E402

REPORT_DIR = os.path.join(ROOT, "reports", "daily")
LOG_DIR = os.path.join(ROOT, "logs")
COLLECT_LOCK = os.path.join(LOG_DIR, "nightly_collect.lock")
SCAN_LOCK = os.path.join(LOG_DIR, "nightly_scan.lock")

N_PICKS = 10
N_SMART_SLOTS = 3
MIN_PRICE = 5.0
MIN_DOLLAR_VOL = 10_000_000       # 10-day avg volume x price
CHART_DAYS = 190                  # ~9 months of bars on the chart

PATTERN_POINTS = {
    "VCP": 8, "Cup & Handle": 8, "MA200 Breakout": 6, "Volume Surge": 6,
    "Momentum Expansion": 5, "Golden Cross": 4, "Pullback Bounce": 4,
    "MA Squeeze": 4, "Momentum 10/8": 3,
}

log = logging.getLogger("brief")


# ═════════════════════════════════════════════════════════════════════════════
# Data loading
# ═════════════════════════════════════════════════════════════════════════════

_PRICE_COLS = ("date, open, high, low, close, volume, ma50, ma150, ma200, high_30d, "
               "high_52wk, low_52wk, vol_ma10, pct_change, rsi14, trend_score")


def _us_ticker(t):
    return t and not t.startswith("^") and "." not in t


def latest_complete_date():
    """Latest stored US trading date whose daily bar is final."""
    from surge_strategy import market_clock
    clock = market_clock()
    with get_connection() as conn:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM universe_prices ORDER BY date DESC LIMIT 3")]
    if dates and dates[0] == clock["today"] and clock["weekday"] and not clock["is_final"]:
        return dates[1]
    return dates[0] if dates else None


def load_prices(as_of):
    start = (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=430)).strftime("%Y-%m-%d")
    by = defaultdict(list)
    with get_connection() as conn:
        for r in conn.execute(f"SELECT ticker, {_PRICE_COLS} FROM stocks_daily "
                              "WHERE date BETWEEN ? AND ? ORDER BY ticker, date", (start, as_of)):
            if _us_ticker(r["ticker"]):
                by[r["ticker"]].append(dict(r))
        tracked = set(by)
        for r in conn.execute(f"SELECT ticker, {_PRICE_COLS} FROM universe_prices "
                              "WHERE date BETWEEN ? AND ? ORDER BY ticker, date", (start, as_of)):
            t = r["ticker"]
            if t not in tracked and _us_ticker(t):
                by[t].append(dict(r))
    return by, tracked


def load_rs(as_of):
    with get_connection() as conn:
        return {r["ticker"]: dict(r) for r in conn.execute(
            "SELECT ticker, rs_rating, rs_leader, rs_line_trend FROM rs_ratings WHERE date = ?",
            (as_of,))}


def load_rs_line(ticker, start):
    with get_connection() as conn:
        return [(r[0], r[1]) for r in conn.execute(
            "SELECT date, rs_line_value FROM rs_ratings WHERE ticker=? AND date>=? "
            "AND rs_line_value IS NOT NULL ORDER BY date", (ticker, start))]


def load_patterns(as_of):
    out = defaultdict(list)
    with get_connection() as conn:
        for r in conn.execute("SELECT ticker, pattern_name, squeeze_fresh FROM pattern_scan_results "
                              "WHERE scan_date = ?", (as_of,)):
            if r["pattern_name"] == "MA Squeeze" and not r["squeeze_fresh"]:
                continue
            if r["pattern_name"] not in out[r["ticker"]]:
                out[r["ticker"]].append(r["pattern_name"])
    return out


def load_surge():
    with get_connection() as conn:
        return {r["ticker"]: dict(r) for r in conn.execute(
            "SELECT ticker, status, surge_date, drop_date, buy_level, trigger_date "
            "FROM surge_signals WHERE status IN ('watching','armed','triggered')")}


def _d(as_of, days):
    return (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")


def load_smart_money(as_of):
    """Per-ticker insider / Congress / 13D / Form 144 activity around as_of."""
    sm = defaultdict(lambda: {"ins_buys": [], "ins_sell_value": 0.0, "ins_sell_n": 0,
                              "ins_sell_planned": 0, "congress_buys": [], "congress_sells": [],
                              "d13": [], "f144": []})
    d30, d45, d14 = _d(as_of, 30), _d(as_of, 45), _d(as_of, 14)
    with get_connection() as conn:
        seen = set()
        q_all = ("SELECT ticker, insider_name, role, transaction_date, shares, price, total_value, "
                 "filing_url FROM insider_trades_all WHERE transaction_code='P' "
                 "AND transaction_date BETWEEN ? AND ? AND ticker IS NOT NULL")
        q_sig = ("SELECT ticker, insider_name, role, transaction_date, shares, price, total_value, "
                 "filing_url FROM insider_signals WHERE transaction_type='Purchase' "
                 "AND transaction_date BETWEEN ? AND ?")
        for q in (q_all, q_sig):
            for r in conn.execute(q, (d30, as_of)):
                key = (r["ticker"], r["insider_name"], r["transaction_date"], r["shares"])
                if key in seen or not (r["total_value"] or 0) > 0:
                    continue
                seen.add(key)
                sm[r["ticker"].upper()]["ins_buys"].append(dict(r))
        for r in conn.execute(
                "SELECT ticker, SUM(total_value) v, COUNT(*) n, SUM(flag_10b51) p FROM insider_trades_all "
                "WHERE transaction_code='S' AND transaction_date BETWEEN ? AND ? "
                "AND ticker IS NOT NULL GROUP BY ticker", (d30, as_of)):
            s = sm[r["ticker"].upper()]
            s["ins_sell_value"], s["ins_sell_n"], s["ins_sell_planned"] = r["v"] or 0, r["n"], r["p"] or 0
        for r in conn.execute(
                "SELECT ticker, politician_name, chamber, party, role, type, tx_date, filed_date, "
                "amount_range FROM congress_trades WHERE ticker IS NOT NULL "
                "AND COALESCE(filed_date, tx_date) BETWEEN ? AND ? "
                "AND tx_date >= ?",              # stale trades filed late don't count
                (d45, as_of, _d(as_of, 120))):
            key = "congress_buys" if r["type"] == "BUY" else "congress_sells" if r["type"] == "SELL" else None
            if key:
                sm[r["ticker"].upper()][key].append(dict(r))
        for r in conn.execute(
                "SELECT ticker, form_type, filer_name, percent_of_class, shares_owned, filed_date, "
                "event_date, purpose, filing_url FROM sec_13dg WHERE form_type LIKE 'SCHEDULE 13D%' "
                "AND filed_date BETWEEN ? AND ? AND ticker IS NOT NULL", (d30, as_of)):
            sm[r["ticker"].upper()]["d13"].append(dict(r))
        for r in conn.execute(
                "SELECT ticker, seller_name, relationship, shares_to_sell, market_value, "
                "approx_sale_date, filed_date, plan_10b51 FROM sec_form144 "
                "WHERE filed_date BETWEEN ? AND ? AND ticker IS NOT NULL", (d14, as_of)):
            sm[r["ticker"].upper()]["f144"].append(dict(r))
    return sm


# ═════════════════════════════════════════════════════════════════════════════
# Scoring
# ═════════════════════════════════════════════════════════════════════════════

def _ret(rows, n):
    if len(rows) <= n or not rows[-1 - n]["close"]:
        return None
    return rows[-1]["close"] / rows[-1 - n]["close"] - 1


def _f(x, d=0.0):
    return d if x is None or (isinstance(x, float) and math.isnan(x)) else x


def score_ticker(t, rows, rs, patterns, surge, sm):
    if len(rows) < 210:
        return None
    r, p = rows[-1], rows[-2]
    close = r["close"]
    if not close or close < MIN_PRICE or not r["vol_ma10"]:
        return None
    if r["vol_ma10"] * close < MIN_DOLLAR_VOL:
        return None

    tech, smart, tags, risks, notes = 0.0, 0.0, [], [], {}
    ts = int(_f(r["trend_score"]))
    tech += ts * 3
    rr = (rs or {}).get("rs_rating")
    if rr and rr >= 50:
        tech += (rr - 50) / 49 * 15
    if (rs or {}).get("rs_leader"):
        tech += 3
        tags.append("RS leader")
    vol_ratio = r["volume"] / r["vol_ma10"] if r["vol_ma10"] else 0
    up = _f(r["pct_change"]) > 0
    if up and vol_ratio >= 2:
        tech += 8
        tags.append(f"Volume {vol_ratio:.1f}x")
    elif up and vol_ratio >= 1.5:
        tech += 4
        tags.append(f"Volume {vol_ratio:.1f}x")
    if _f(r["pct_change"]) >= 3:
        tech += 3
    brk30 = bool(p["high_30d"] and close > p["high_30d"])
    if brk30:
        tech += 6
        tags.append("30-day breakout")
    at_high = bool(r["high_52wk"] and close >= 0.98 * r["high_52wk"])
    if at_high:
        tech += 4
        tags.append("At 52-wk high")
    elif r["high_52wk"] and close >= 0.90 * r["high_52wk"]:
        tech += 3
    pat_pts = sum(PATTERN_POINTS.get(x, 3) for x in patterns)
    tech += min(pat_pts, 15)
    tags += patterns
    if surge:
        if surge["status"] in ("armed", "triggered"):
            tech += 6
            tags.append("Surge: " + ("drop day" if surge["status"] == "armed" else "buy signal"))
        else:
            tech += 3
            tags.append("Surge: watching")
    dist50 = close / r["ma50"] - 1 if r["ma50"] else None
    rsi = r["rsi14"]
    if dist50 is not None and dist50 > 0.25:
        tech -= 5
        risks.append(f"Extended {dist50:+.0%} above the 50-day MA")
    if rsi and rsi > 80:
        tech -= 4
        risks.append(f"RSI {rsi:.0f} (overbought)")

    # smart money
    buys = sm.get("ins_buys", []) if sm else []
    buy_val = sum(b["total_value"] or 0 for b in buys)
    buyers = {b["insider_name"] for b in buys}
    if buy_val >= 1_000_000:
        smart += 12
    elif buy_val >= 250_000:
        smart += 8
    elif buy_val >= 50_000:
        smart += 4
    if len(buyers) >= 3:
        smart += 6
        tags.append(f"Insider cluster buy ({len(buyers)})")
    elif buys:
        smart += 3 if len(buyers) == 2 else 0
        tags.append(f"Insider buying ${buy_val/1e6:.2f}M")
    cbuys = sm.get("congress_buys", []) if sm else []
    pols = {c["politician_name"] for c in cbuys}
    if pols:
        smart += 10 if len(pols) >= 3 else 8 if len(pols) == 2 else 5
        tags.append(f"Congress buy ({len(pols)})")
    d13 = sm.get("d13", []) if sm else []
    if any(x["form_type"] == "SCHEDULE 13D" for x in d13):
        smart += 8
        tags.append("New 13D stake")
    elif d13:
        smart += 4
        tags.append("13D amendment")
    sell_val = sm.get("ins_sell_value", 0) if sm else 0
    if sell_val >= 10_000_000:
        smart -= 3
        risks.append(f"Insider selling ${sell_val/1e6:.1f}M in 30 days")
    f144_val = sum(_f(x["market_value"]) for x in (sm.get("f144", []) if sm else []))
    if f144_val >= 5_000_000:
        smart -= 2
        risks.append(f"Form 144 notices: ${f144_val/1e6:.1f}M planned sales (14 days)")

    notes.update(dict(
        ticker=t, close=close, pct=_f(r["pct_change"]), vol_ratio=vol_ratio, trend_score=ts,
        rs_rating=rr, rs_leader=bool((rs or {}).get("rs_leader")),
        rs_trend=(rs or {}).get("rs_line_trend"), rsi=rsi, dist50=dist50,
        ma50=r["ma50"], ma150=r["ma150"], ma200=r["ma200"], high52=r["high_52wk"],
        low52=r["low_52wk"], brk30=brk30, at_high=at_high,
        ret5=_ret(rows, 5), ret21=_ret(rows, 21), ret63=_ret(rows, 63),
        dollar_vol=r["vol_ma10"] * close, patterns=patterns, surge=surge,
        buy_val=buy_val, buyers=sorted(buyers), congress_pols=sorted(pols),
        sell_val=sell_val, f144_val=f144_val,
    ))
    return {"ticker": t, "tech": round(tech, 1), "smart": round(smart, 1),
            "score": round(tech + smart, 1), "tags": tags, "risks": risks, "m": notes}


def select_picks(scored):
    ranked = sorted(scored, key=lambda s: s["score"], reverse=True)
    picks = ranked[:N_PICKS - N_SMART_SLOTS]
    chosen = {s["ticker"] for s in picks}
    smart = [s for s in sorted(scored, key=lambda s: (s["smart"], s["score"]), reverse=True)
             if s["ticker"] not in chosen and s["smart"] >= 8
             and s["m"]["ma200"] and s["m"]["close"] > s["m"]["ma200"] and s["m"]["trend_score"] >= 4]
    for s in smart[:N_SMART_SLOTS]:
        s["slot"] = "smart"
        picks.append(s)
        chosen.add(s["ticker"])
    for s in ranked:
        if len(picks) >= N_PICKS:
            break
        if s["ticker"] not in chosen:
            picks.append(s)
            chosen.add(s["ticker"])
    picks.sort(key=lambda s: s["score"], reverse=True)
    return picks


# ═════════════════════════════════════════════════════════════════════════════
# External: news, fundamentals
# ═════════════════════════════════════════════════════════════════════════════

NEWS_TAGS = [
    ("Earnings", r"earnings|quarter|q[1-4]\b|eps|revenue|results|guidance|outlook"),
    ("Upgrade", r"upgrade|raises? (?:price )?target|outperform|overweight|buy rating"),
    ("Downgrade", r"downgrade|cuts? (?:price )?target|underperform|underweight|sell rating"),
    ("M&A", r"acqui|merger|takeover|buyout|deal to buy|to be acquired"),
    ("Regulatory", r"\bfda\b|approval|sec\b|antitrust|probe|investigation"),
    ("Legal", r"lawsuit|class action|sued|settlement"),
    ("Capital", r"offering|buyback|repurchase|dividend|stock split"),
    ("Insider", r"insider|ceo buys|director buys|stake"),
]


def fetch_news(ticker, company, days=7, limit=6):
    q = f'"{ticker}" stock' if not company else f'"{ticker}" OR "{_short_name(company)}" stock'
    try:
        r = requests.get("https://news.google.com/rss/search",
                         params={"q": f"{q} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"},
                         headers={"User-Agent": "Mozilla/5.0 (stock-dashboard brief)"}, timeout=20)
        root = ET.fromstring(r.content)
    except Exception as exc:
        log.warning("news %s: %s", ticker, exc)
        return []
    items = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        src = (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        try:
            dt = datetime.strptime(it.findtext("pubDate") or "", "%a, %d %b %Y %H:%M:%S %Z")
        except ValueError:
            dt = None
        tags = [n for n, rx in NEWS_TAGS if re.search(rx, title, re.I)]
        items.append({"title": title, "source": src, "link": it.findtext("link"),
                      "date": dt, "tags": tags})
    items.sort(key=lambda x: x["date"] or datetime.min, reverse=True)
    # de-duplicate near-identical headlines
    out, seen = [], set()
    for x in items:
        k = re.sub(r"\W+", "", x["title"].lower())[:60]
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out[:limit]


def _short_name(name):
    n = re.sub(r"[,.]?\s+(inc|corp|corporation|co|company|ltd|plc|holdings|group|class [a-z])\.?$",
               "", name.strip(), flags=re.I)
    return n.split(",")[0][:40]


def fetch_fundamentals(ticker):
    out = {}
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info or {}
        for k in ("shortName", "longName", "sector", "industry", "marketCap", "trailingPE",
                  "forwardPE", "targetMeanPrice", "recommendationKey", "numberOfAnalystOpinions",
                  "revenueGrowth", "earningsGrowth", "profitMargins", "beta"):
            out[k] = info.get(k)
        try:
            cal = t.calendar or {}
            ed = cal.get("Earnings Date")
            if ed:
                out["next_earnings"] = ed[0] if isinstance(ed, (list, tuple)) else ed
        except Exception:
            pass
    except Exception as exc:
        log.warning("fundamentals %s: %s", ticker, exc)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Chart
# ═════════════════════════════════════════════════════════════════════════════

def _sma(vals, n):
    out, s = [], 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def make_chart(pick, rows, levels, sm, rs_line):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    rows = rows[-CHART_DAYS:]
    full_close = [r["close"] for r in rows]
    dts = [datetime.strptime(r["date"], "%Y-%m-%d") for r in rows]
    x = mdates.date2num(dts)
    ma21 = _sma(full_close, 21)

    fig = plt.figure(figsize=(11, 6.6), dpi=105)
    gs = fig.add_gridspec(3, 1, height_ratios=[3.2, 1, 1], hspace=0.06)
    ax, axv, axr = fig.add_subplot(gs[0]), fig.add_subplot(gs[1]), fig.add_subplot(gs[2])
    for a in (ax, axv, axr):
        a.set_facecolor("#ffffff")
        a.grid(True, color="#e6e9ef", lw=0.6)
        for s in ("top", "right"):
            a.spines[s].set_visible(False)
        a.spines["left"].set_color("#9aa5b1")
        a.spines["bottom"].set_color("#9aa5b1")
        a.tick_params(colors="#3e4c59", labelsize=8)

    w = 0.6
    for xi, r in zip(x, rows):
        o, h, l, c = r["open"] or r["close"], r["high"] or r["close"], r["low"] or r["close"], r["close"]
        col = "#1f8a5b" if c >= o else "#c0392b"
        ax.vlines(xi, l, h, color=col, lw=0.7)
        ax.add_patch(plt.Rectangle((xi - w / 2, min(o, c)), w, max(abs(c - o), 1e-6),
                                   color=col, lw=0))
    series = [("MA21", ma21, "#f39c12", 1.0), ("MA50", [r["ma50"] for r in rows], "#2563eb", 1.2),
              ("MA150", [r["ma150"] for r in rows], "#8e44ad", 1.0),
              ("MA200", [r["ma200"] for r in rows], "#111827", 1.3)]
    for name, ys, col, lw in series:
        pts = [(xi, y) for xi, y in zip(x, ys) if y]
        if pts:
            ax.plot(*zip(*pts), color=col, lw=lw, label=name)
    if rows[-1]["high_52wk"]:
        ax.axhline(rows[-1]["high_52wk"], color="#9aa5b1", ls="--", lw=0.8)
        ax.text(x[-1] + 1.5, rows[-1]["high_52wk"], "52-wk high", va="bottom", fontsize=7, color="#52606d")
    lo, hi = min(r["low"] or r["close"] for r in rows), max(r["high"] or r["close"] for r in rows)
    for lv in levels[:4]:
        price = lv["price"]
        if lo * 0.97 <= price <= hi * 1.03:
            col = "#1f8a5b" if lv["kind"] == "support" else "#c0392b"
            ax.axhline(price, color=col, ls=":", lw=1.0, alpha=0.8)
            ax.text(x[-1] + 1.5, price, f"{lv['kind'][0].upper()} {price:,.2f}", va="center",
                    fontsize=7, color=col)

    def mark(date_s, y, marker, color, label):
        try:
            d = mdates.date2num(datetime.strptime(date_s[:10], "%Y-%m-%d"))
        except (TypeError, ValueError):
            return
        if x[0] <= d <= x[-1]:
            ax.scatter([d], [y], marker=marker, s=60, color=color, edgecolor="white",
                       lw=0.6, zorder=5, label=label)

    lows = {r["date"]: r["low"] or r["close"] for r in rows}
    highs = {r["date"]: r["high"] or r["close"] for r in rows}

    def near(dmap, ds, default):
        if ds in dmap:
            return dmap[ds]
        later = [k for k in dmap if k >= (ds or "")]
        return dmap[min(later)] if later else default

    used = set()
    for b in sm.get("ins_buys", []):
        lab = "Insider buy" if "ib" not in used else None
        used.add("ib")
        mark(b["transaction_date"], near(lows, b["transaction_date"], lo) * 0.975, "^", "#16a34a", lab)
    for c in sm.get("congress_buys", []):
        lab = "Congress buy" if "cb" not in used else None
        used.add("cb")
        mark(c["tx_date"], near(lows, c["tx_date"], lo) * 0.95, "D", "#7c3aed", lab)
    for c in sm.get("congress_sells", []):
        lab = "Congress sell" if "cs" not in used else None
        used.add("cs")
        mark(c["tx_date"], near(highs, c["tx_date"], hi) * 1.03, "D", "#db2777", lab)
    for f in sm.get("f144", []):
        lab = "Form 144 (planned sale)" if "f1" not in used else None
        used.add("f1")
        mark(f["filed_date"], near(highs, f["filed_date"], hi) * 1.025, "v", "#ea580c", lab)
    for d in sm.get("d13", []):
        lab = "13D filing" if "13" not in used else None
        used.add("13")
        mark(d["filed_date"], near(lows, d["filed_date"], lo) * 0.96, "*", "#0284c7", lab)

    m = pick["m"]
    ax.set_title(f"{pick['ticker']}  ·  {pick.get('name') or ''}   Close {m['close']:,.2f}  ({m['pct']:+.2f}%)",
                 loc="left", fontsize=11, color="#0b2545", fontweight="bold")
    ax.legend(loc="upper left", fontsize=7, ncol=5, frameon=False)
    ax.set_xlim(x[0] - 1, x[-1] + 9)
    plt.setp(ax.get_xticklabels(), visible=False)

    vols = [r["volume"] or 0 for r in rows]
    vcol = ["#1f8a5b" if (r["close"] >= (r["open"] or r["close"])) else "#c0392b" for r in rows]
    axv.bar(x, vols, width=0.7, color=vcol, alpha=0.55)
    vm = [(xi, r["vol_ma10"]) for xi, r in zip(x, rows) if r["vol_ma10"]]
    if vm:
        axv.plot(*zip(*vm), color="#334e68", lw=1.0, label="Vol MA10")
    axv.set_ylabel("Volume", fontsize=8, color="#52606d")
    axv.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(
        lambda v, _: f"{v/1e6:.0f}M" if v >= 1e6 else f"{v/1e3:.0f}K"))
    axv.set_xlim(ax.get_xlim())
    plt.setp(axv.get_xticklabels(), visible=False)

    if rs_line:
        rx = [mdates.date2num(datetime.strptime(d, "%Y-%m-%d")) for d, _ in rs_line]
        axr.plot(rx, [v for _, v in rs_line], color="#0b7285", lw=1.2)
        axr.set_ylabel("RS line vs S&P", fontsize=8, color="#52606d")
    else:
        rsi = [(xi, r["rsi14"]) for xi, r in zip(x, rows) if r["rsi14"]]
        if rsi:
            axr.plot(*zip(*rsi), color="#0b7285", lw=1.0)
            axr.axhline(70, color="#c0392b", lw=0.6, ls="--")
            axr.axhline(30, color="#1f8a5b", lw=0.6, ls="--")
        axr.set_ylabel("RSI 14", fontsize=8, color="#52606d")
    axr.set_xlim(ax.get_xlim())
    axr.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


# ═════════════════════════════════════════════════════════════════════════════
# Market context
# ═════════════════════════════════════════════════════════════════════════════

def market_context(by, as_of):
    ctx = {"as_of": as_of}
    try:
        import market_health
        mh = market_health.get_market_health()
        ctx["health"] = mh.get("status") or {}
        ctx["dd"] = {k: v.get("dd_count") for k, v in mh.get("indices", {}).items()}
    except Exception as exc:
        log.warning("market health: %s", exc)
        ctx["health"] = {}
    ctx["index"] = {}
    try:
        import market_health
        idx = market_health.fetch_index_data(["^GSPC", "^IXIC", "^RUT", "^VIX"], period="1y")
        ctx["index_df"] = idx
        for sym, df in idx.items():
            df = df[df.index <= as_of + " 23:59"]
            c = df["Close"]
            if len(c) > 22:
                ctx["index"][sym] = {"close": float(c.iloc[-1]), "d1": float(c.iloc[-1] / c.iloc[-2] - 1),
                                     "d5": float(c.iloc[-1] / c.iloc[-6] - 1),
                                     "d21": float(c.iloc[-1] / c.iloc[-22] - 1)}
    except Exception as exc:
        log.warning("index data: %s", exc)
    n = a50 = a200 = hi = lo = tt = 0
    for rows in by.values():
        r = rows[-1]
        if r["date"] != as_of or not r["close"]:
            continue
        n += 1
        a50 += bool(r["ma50"] and r["close"] > r["ma50"])
        a200 += bool(r["ma200"] and r["close"] > r["ma200"])
        hi += bool(r["high_52wk"] and r["close"] >= 0.98 * r["high_52wk"])
        lo += bool(r["low_52wk"] and r["close"] <= 1.02 * r["low_52wk"])
        tt += bool((r["trend_score"] or 0) >= 7)
    ctx["breadth"] = {"n": n, "above50": a50 / n if n else None, "above200": a200 / n if n else None,
                      "highs": hi, "lows": lo, "tt7": tt}
    return ctx


def smart_money_monitor(as_of):
    d7, d3 = _d(as_of, 7), _d(as_of, 3)
    out = {}
    with get_connection() as conn:
        out["ins_buys"] = [dict(r) for r in conn.execute(
            "SELECT ticker, MAX(company_name) company, COUNT(DISTINCT insider_name) n, SUM(total_value) v, "
            "GROUP_CONCAT(DISTINCT role) roles, MAX(transaction_date) last "
            "FROM insider_trades_all WHERE transaction_code='P' AND filed_date >= ? AND ticker IS NOT NULL "
            "GROUP BY ticker ORDER BY v DESC LIMIT 10", (d7,))]
        out["congress"] = [dict(r) for r in conn.execute(
            "SELECT ticker, type, COUNT(*) n, COUNT(DISTINCT politician_name) pols, "
            "GROUP_CONCAT(DISTINCT politician_name) names, MAX(filed_date) filed "
            "FROM congress_trades WHERE filed_date >= ? AND ticker IS NOT NULL "
            "GROUP BY ticker, type ORDER BY pols DESC, n DESC LIMIT 12", (d7,))]
        out["d13"] = [dict(r) for r in conn.execute(
            "SELECT ticker, company_name, form_type, filer_name, percent_of_class, filed_date "
            "FROM sec_13dg WHERE form_type='SCHEDULE 13D' AND filed_date >= ? "
            "ORDER BY percent_of_class DESC LIMIT 10", (d7,))]
        out["f144"] = [dict(r) for r in conn.execute(
            "SELECT ticker, company_name, seller_name, relationship, market_value, approx_sale_date, "
            "plan_10b51 FROM sec_form144 WHERE filed_date >= ? ORDER BY market_value DESC LIMIT 10", (d3,))]
    return out


# ═════════════════════════════════════════════════════════════════════════════
# History (previous reports)
# ═════════════════════════════════════════════════════════════════════════════

def previous_appearances(tickers, as_of):
    out = defaultdict(list)
    with get_connection() as conn:
        for r in conn.execute(
                f"SELECT ticker, as_of, rank, score, close FROM daily_report_picks "
                f"WHERE as_of < ? AND ticker IN ({','.join('?' * len(tickers))}) ORDER BY as_of",
                (as_of, *tickers)):
            out[r["ticker"]].append(dict(r))
    return out


def scorecard(by, ctx, as_of, n_reports=10):
    with get_connection() as conn:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT as_of FROM daily_report_picks WHERE as_of < ? ORDER BY as_of DESC LIMIT ?",
            (as_of, n_reports))]
        if not dates:
            return []
        rows = [dict(r) for r in conn.execute(
            f"SELECT as_of, rank, ticker, close, score FROM daily_report_picks "
            f"WHERE as_of IN ({','.join('?' * len(dates))}) ORDER BY as_of DESC, rank", dates)]
    spx = ctx.get("index_df", {}).get("^GSPC")
    for r in rows:
        last = by.get(r["ticker"], [{}])[-1].get("close")
        r["now"] = last
        r["ret"] = last / r["close"] - 1 if last and r["close"] else None
        r["spx"] = None
        if spx is not None:
            try:
                a = spx[spx.index <= r["as_of"] + " 23:59"]["Close"].iloc[-1]
                b = spx[spx.index <= as_of + " 23:59"]["Close"].iloc[-1]
                r["spx"] = float(b / a - 1)
            except Exception:
                pass
    return rows


def save_picks(as_of, picks):
    with get_connection() as conn:
        conn.execute("DELETE FROM daily_report_picks WHERE as_of = ?", (as_of,))
        for i, p in enumerate(picks, 1):
            lv = (p.get("nar") or {}).get("levels") or {}
            conn.execute(
                "INSERT INTO daily_report_picks (as_of, rank, ticker, score, tech_score, smart_score, "
                "close, headline, tags, risks, stop_level, support_level, resistance_level) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (as_of, i, p["ticker"], p["score"], p["tech"], p["smart"], p["m"]["close"],
                 p.get("headline"), json.dumps(p["tags"]), json.dumps(p["risks"]),
                 lv.get("stop"), (lv.get("s1") or {}).get("price"), (lv.get("r1") or {}).get("price")))
        conn.commit()


# ═════════════════════════════════════════════════════════════════════════════
# Analyst narrative (rule-based, from the facts above)
# ═════════════════════════════════════════════════════════════════════════════

def _pct(x, signed=True):
    return "—" if x is None else (f"{x:+.1%}" if signed else f"{x:.1%}")


def _money(v):
    if v is None:
        return "—"
    v = float(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v/div:,.1f}{suf}"
    return f"${v:,.0f}"


def grade(p):
    s = p["score"]
    return "A" if s >= 60 else "B" if s >= 45 else "C"


def build_narrative(p, fund, news, levels, prev, regime):
    m, name = p["m"], p.get("name") or p["ticker"]
    tech_bits, smart_bits = [], []
    stage = ("a confirmed Stage-2 uptrend (Trend Template " + f"{m['trend_score']}/8)"
             if m["trend_score"] >= 7 else
             f"an improving trend (Trend Template {m['trend_score']}/8)" if m["trend_score"] >= 5 else
             f"a base / early-repair phase (Trend Template {m['trend_score']}/8)")
    rs_txt = (f"RS Rating {m['rs_rating']:.0f}" if m["rs_rating"] else "RS n/a")
    if m["rs_trend"] == "up":
        rs_txt += ", RS line rising"
    tech_bits.append(f"Trades in {stage}; {rs_txt}.")
    trig = []
    if m["brk30"]:
        trig.append("closed above its 30-day high")
    if m["at_high"]:
        trig.append("is pressing its 52-week high")
    if m["vol_ratio"] >= 1.5 and m["pct"] > 0:
        trig.append(f"advanced {m['pct']:+.1f}% on {m['vol_ratio']:.1f}x average volume (institutional demand)")
    if m["patterns"]:
        trig.append("triggered " + ", ".join(m["patterns"]))
    if m["surge"]:
        s = m["surge"]
        if s["status"] == "armed":
            trig.append(f"is on a Surge-strategy Drop Day (buy level {s['buy_level']:.2f})")
        elif s["status"] == "triggered":
            trig.append(f"triggered a Surge-strategy buy at {s['buy_level']:.2f}")
        else:
            trig.append(f"is on the Surge watch list (surge day {s['surge_date']})")
    if trig:
        tech_bits.append("The stock " + "; ".join(trig) + ".")
    perf = f"Performance: 1W {_pct(m['ret5'])}, 1M {_pct(m['ret21'])}, 3M {_pct(m['ret63'])}."
    tech_bits.append(perf)

    if m["buy_val"]:
        smart_bits.append(f"Open-market insider purchases of {_money(m['buy_val'])} in the last 30 days by "
                          f"{len(m['buyers'])} insider(s) ({', '.join(m['buyers'][:3])}"
                          f"{'…' if len(m['buyers']) > 3 else ''}).")
    if m["congress_pols"]:
        smart_bits.append(f"Disclosed purchases by {len(m['congress_pols'])} member(s) of Congress / officials: "
                          f"{', '.join(m['congress_pols'][:3])}{'…' if len(m['congress_pols']) > 3 else ''}.")
    for d in p.get("_d13", [])[:2]:
        smart_bits.append(f"{d['filer_name']} filed a {d['form_type'].replace('SCHEDULE ', '')} "
                          f"({_pct((d['percent_of_class'] or 0)/100, False)} of the class) on {d['filed_date']}.")
    if not smart_bits:
        smart_bits.append("No notable insider, Congressional or activist activity in the look-back window — "
                          "this is a price/volume-driven idea.")

    street = []
    if fund.get("marketCap"):
        street.append(f"Market cap {_money(fund['marketCap'])}")
    if fund.get("forwardPE"):
        street.append(f"forward P/E {fund['forwardPE']:.1f}x")
    if fund.get("revenueGrowth") is not None:
        street.append(f"revenue growth {_pct(fund['revenueGrowth'])}")
    if fund.get("targetMeanPrice") and m["close"]:
        up = fund["targetMeanPrice"] / m["close"] - 1
        rec = (fund.get("recommendationKey") or "").replace("_", " ")
        street.append(f"consensus target {fund['targetMeanPrice']:,.2f} ({_pct(up)} vs. last), "
                      f"rating '{rec}' from {fund.get('numberOfAnalystOpinions') or '?'} analysts")

    sup = [l for l in levels if l["kind"] == "support" and l["price"] < m["close"]]
    res = [l for l in levels if l["kind"] == "resistance" and l["price"] > m["close"]]
    s1 = max(sup, key=lambda l: l["price"]) if sup else None
    r1 = min(res, key=lambda l: l["price"]) if res else None
    stop_ref = s1["price"] if s1 else (m["ma50"] or m["close"] * 0.92)
    stop = min(stop_ref * 0.985, m["close"] * 0.97)
    if m["close"] - stop > m["close"] * 0.10:
        stop = m["close"] * 0.92
    lv = {"s1": s1, "r1": r1, "stop": stop, "risk_pct": stop / m["close"] - 1}

    risks = list(p["risks"])
    ne = fund.get("next_earnings")
    if ne:
        try:
            ned = ne if isinstance(ne, date) else datetime.strptime(str(ne)[:10], "%Y-%m-%d").date()
            days = (ned - datetime.strptime(regime["as_of"], "%Y-%m-%d").date()).days
            if 0 <= days <= 14:
                risks.append(f"Earnings in {days} day{'s' if days != 1 else ''} ({ned:%d %b}) — binary event risk; size accordingly")
        except Exception:
            pass
    if regime.get("health", {}).get("status") in ("correction", "caution"):
        risks.append(f"Market regime: {regime['health'].get('label')} — breakouts fail more often; "
                     "favour smaller initial size")
    neg_news = [n for n in news if {"Downgrade", "Legal"} & set(n["tags"])]
    if neg_news:
        risks.append("Negative headline flow: " + neg_news[0]["title"][:90])

    catalysts = [n for n in news if n["tags"]]
    if catalysts:
        cat = f"Recent news flow: {catalysts[0]['title'][:110]} ({catalysts[0]['source']})."
    elif news:
        cat = f"Latest headline: {news[0]['title'][:110]} ({news[0]['source']})."
    else:
        cat = "No company-specific headlines in the past week."

    drivers = []
    if m["brk30"] or m["at_high"]:
        drivers.append("breakout")
    if m["patterns"]:
        drivers.append(m["patterns"][0])
    if m["buy_val"] or m["congress_pols"] or p.get("_d13"):
        drivers.append("smart-money accumulation")
    if m["rs_leader"]:
        drivers.append("RS leadership")
    if not drivers:
        drivers.append("trend strength")
    headline = (f"{' + '.join(d.capitalize() if i == 0 else d for i, d in enumerate(drivers[:3]))}"
                f" — {'Stage-2 leader' if m['trend_score'] >= 7 else 'emerging setup'}")
    history = None
    if prev:
        first = prev[0]
        ch = m["close"] / first["close"] - 1 if first["close"] else None
        history = (f"Featured {len(prev)} time(s) before (first {first['as_of']} at {first['close']:,.2f}; "
                   f"{_pct(ch)} since). " + ("Still qualifying — momentum intact."
                                             if ch is not None and ch > 0 else "Re-qualifies after a pullback."))
    return {"headline": headline, "tech": tech_bits, "smart": smart_bits, "street": street,
            "catalyst": cat, "risks": risks, "levels": lv, "history": history}


# ═════════════════════════════════════════════════════════════════════════════
# HTML
# ═════════════════════════════════════════════════════════════════════════════

CSS = """
:root{--navy:#0b2545;--ink:#1f2933;--muted:#616e7c;--line:#e4e7eb;--bg:#f5f7fa;--gold:#b08900;
--green:#1f8a5b;--red:#c0392b;--card:#fff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 "Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px 40px}
.mast{background:var(--navy);color:#fff;padding:22px 0 18px;border-bottom:4px solid var(--gold)}
.mast h1{margin:0;font:600 26px/1.2 Georgia,"Times New Roman",serif;letter-spacing:.3px}
.mast .sub{color:#c8d3e0;font-size:13px;margin-top:4px}
h2{font:600 19px Georgia,serif;color:var(--navy);border-bottom:2px solid var(--navy);
padding-bottom:4px;margin:30px 0 12px}
h3{margin:0;font-size:17px;color:var(--navy)}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:16px 18px;margin:14px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:12px 14px}
.kpi .l{font-size:11px;text-transform:uppercase;color:var(--muted);letter-spacing:.5px}
.kpi .v{font-size:20px;font-weight:600;margin-top:2px}
.pos{color:var(--green)}.neg{color:var(--red)}.mut{color:var(--muted)}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}
th{background:#eef2f7;color:var(--navy);text-align:left;font-weight:600;padding:7px 8px;
border-bottom:1px solid #cbd2d9;white-space:nowrap}
td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
td.r,th.r{text-align:right}
.tag{display:inline-block;background:#e8eef7;color:#243b53;border-radius:3px;padding:0 6px;
margin:1px 3px 1px 0;font-size:11px;white-space:nowrap}
.tag.s{background:#efe7fb;color:#5b21b6}.tag.w{background:#fdecea;color:#9b1c1c}
.grade{display:inline-block;width:26px;height:26px;border-radius:50%;text-align:center;
line-height:26px;font-weight:700;color:#fff;margin-right:8px}
.gA{background:#1f8a5b}.gB{background:#2563eb}.gC{background:#8a94a6}
.regime{border-left:6px solid var(--gold);padding:12px 16px;background:#fff;border-radius:4px}
.regime.red{border-color:var(--red)}.regime.green{border-color:var(--green)}.regime.amber{border-color:#d97706}
.pick h3 small{font-weight:400;color:var(--muted);font-size:13px}
.cols{display:grid;grid-template-columns:1.25fr 1fr;gap:18px}
@media(max-width:820px){.cols{grid-template-columns:1fr}.mast h1{font-size:21px}}
.sec-t{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--gold);
font-weight:700;margin:10px 0 3px}
ul{margin:4px 0 4px 18px;padding:0}li{margin:2px 0}
img.chart{width:100%;height:auto;border:1px solid var(--line);border-radius:4px;margin-top:10px}
.lv{display:flex;gap:10px;flex-wrap:wrap;margin-top:6px}
.lv div{background:#f7f9fb;border:1px solid var(--line);border-radius:4px;padding:6px 10px;font-size:12px}
.news a{color:#1d4ed8;text-decoration:none}.news li{margin:4px 0}
.disc{font-size:11.5px;color:var(--muted);margin-top:28px;border-top:1px solid var(--line);padding-top:10px}
a.toc{color:var(--navy);text-decoration:none;font-weight:600}
"""


def _e(s):
    return html.escape(str(s)) if s is not None else ""


def _cls(x):
    return "pos" if (x or 0) > 0 else "neg" if (x or 0) < 0 else "mut"


# Insider / Congress activity badges (same icons as the dashboard: 🏛 Congress,
# 💼▲n insider open-market buys / 💼▼n sales only). Counts are frozen at report
# time (windows end on as_of); clicking opens a live popup from the dashboard API.
INSIDER_BADGE_DAYS = 180
CONGRESS_BADGE_DAYS = 365


def load_activity(as_of):
    since_i = (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=INSIDER_BADGE_DAYS)).strftime("%Y-%m-%d")
    since_c = (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=CONGRESS_BADGE_DAYS)).strftime("%Y-%m-%d")
    ins, cong = {}, {}
    with get_connection() as conn:
        for r in conn.execute(
                """SELECT ticker, SUM(transaction_code='P'), SUM(CASE WHEN transaction_code='P' THEN total_value END),
                          SUM(transaction_code='S'), SUM(CASE WHEN transaction_code='S' THEN total_value END),
                          MAX(transaction_date)
                   FROM insider_trades_all WHERE ticker IS NOT NULL AND transaction_code IN ('P','S')
                     AND transaction_date >= ? AND transaction_date <= ? GROUP BY ticker""", (since_i, as_of)):
            ins[r[0]] = (r[1] or 0, r[2] or 0, r[3] or 0, r[4] or 0, r[5])
        for r in conn.execute(
                """SELECT ticker, SUM(transaction_type='Purchase'), SUM(CASE WHEN transaction_type='Purchase' THEN total_value END),
                          SUM(transaction_type='Sale'), SUM(CASE WHEN transaction_type='Sale' THEN total_value END),
                          MAX(transaction_date)
                   FROM insider_signals WHERE ticker IS NOT NULL AND transaction_date >= ? AND transaction_date <= ?
                   GROUP BY ticker""", (since_i, as_of)):
            ins.setdefault(r[0], (r[1] or 0, r[2] or 0, r[3] or 0, r[4] or 0, r[5]))
        for r in conn.execute(
                """SELECT ticker, SUM(type='BUY'), SUM(type='SELL'), COUNT(DISTINCT politician_name), MAX(tx_date)
                   FROM congress_trades WHERE ticker IS NOT NULL AND tx_date >= ? AND tx_date <= ?
                   GROUP BY ticker""", (since_c, as_of)):
            cong[r[0]] = (r[1] or 0, r[2] or 0, r[3] or 0, r[4])
    return {"ins": ins, "cong": cong}


def _badges(t, act):
    if not t or not act:
        return ""
    out = []
    c = act["cong"].get(t)
    if c:
        tip = (f"Congress / executive branch, last 12 months: {c[0]} buys, {c[1]} sells by {c[2]} "
               f"official{'s' if c[2] != 1 else ''}; latest {c[3]} - click for details")
        out.append(f"<span class='bdg' title='{_e(tip)}' onclick=\"mbPop('{_e(t)}','congress')\">&#127963;</span>")
    i = act["ins"].get(t)
    if i:
        b, bv, sl, sv, last = i
        tip = (f"Insider trading, last {INSIDER_BADGE_DAYS} days: {b} buys ({_money(bv)}), {sl} sales "
               f"({_money(sv)}); latest {last} - click for details")
        lab = f"&#128188;&#9650;{b}" if b else f"&#128188;&#9660;{sl}"
        out.append(f"<span class='bdg {'ib' if b else 'is'}' title='{_e(tip)}' "
                   f"onclick=\"mbPop('{_e(t)}','insider')\">{lab}</span>")
    return " " + "".join(out) if out else ""


BADGE_CSS = """
.bdg{cursor:pointer;margin-left:4px;font-size:11px;font-weight:700;white-space:nowrap}
.bdg.ib{color:#15803d}.bdg.is{color:#b91c1c;opacity:.8}
#mbpop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:50}
#mbpop .box{background:#fff;color:#111;max-width:1000px;width:95%;max-height:85vh;overflow:auto;margin:6vh auto;
  border-radius:10px;padding:18px 20px;font-size:13px}
#mbpop table{width:100%;border-collapse:collapse}#mbpop td,#mbpop th{padding:4px 6px;border-bottom:1px solid #eee;text-align:left}
#mbpop .x{float:right;cursor:pointer;border:1px solid #ccc;border-radius:6px;padding:2px 10px;background:#f7f7f7}
"""

BADGE_JS = r"""
<div id='mbpop' onclick="if(event.target===this)this.style.display='none'"><div class='box'>
<span class='x' onclick="document.getElementById('mbpop').style.display='none'">&#x2715; Close</span>
<div id='mbpop-t' style='font-weight:700;font-size:15px;margin-bottom:8px'></div><div id='mbpop-b'></div></div></div>
<script>
function mbEsc(v){return v==null?'':String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function mbMoney(v){if(v==null)return '—';const a=Math.abs(v);return a>=1e9?'$'+(v/1e9).toFixed(1)+'B':a>=1e6?'$'+(v/1e6).toFixed(1)+'M':a>=1e3?'$'+(v/1e3).toFixed(0)+'K':'$'+Math.round(v);}
async function mbPop(t,kind){
  const box=document.getElementById('mbpop'),T=document.getElementById('mbpop-t'),B=document.getElementById('mbpop-b');
  T.textContent=t+(kind==='insider'?' — Insider trading (Form 4, last 12 months, buys & sales)':' — Congress / executive-branch trades');
  B.innerHTML='Loading…';box.style.display='block';
  try{
    const url=kind==='insider'?'/api/insider/ticker/'+encodeURIComponent(t)+'?days=365':'/api/congress?ticker='+encodeURIComponent(t);
    const r=await fetch(url);if(!r.ok)throw new Error(r.status);const rows=(await r.json()).data||[];
    if(!rows.length){B.innerHTML='No transactions found.';return;}
    if(kind==='insider'){
      B.innerHTML='<table><tr><th>Tx date</th><th>Filed</th><th>Insider</th><th>Role</th><th>Type</th><th>Shares</th><th>Price</th><th>Value</th><th>10b5-1</th><th></th></tr>'+
        rows.map(x=>{const buy=x.transaction_code==='P'||(!x.transaction_code&&x.transaction_type==='Purchase');
          return '<tr><td>'+mbEsc(x.transaction_date)+'</td><td>'+mbEsc(x.filed_date)+'</td><td>'+mbEsc(x.insider_name)+'</td><td>'+mbEsc(x.role)+
          '</td><td style="color:'+(buy?'#15803d':'#b91c1c')+';font-weight:600">'+mbEsc(x.transaction_type)+'</td><td>'+(x.shares!=null?Math.round(x.shares).toLocaleString():'—')+
          '</td><td>'+(x.price!=null?'$'+x.price.toFixed(2):'—')+'</td><td><b>'+mbMoney(x.total_value)+'</b></td><td>'+(x.flag_10b51?'✓':'')+
          '</td><td>'+(x.filing_url?'<a target="_blank" href="'+mbEsc(x.filing_url)+'">SEC ↗</a>':'')+'</td></tr>';}).join('')+'</table>';
    }else{
      B.innerHTML='<table><tr><th>Tx date</th><th>Filed</th><th>Official</th><th>Chamber</th><th>Party</th><th>Type</th><th>Amount</th><th></th></tr>'+
        rows.map(x=>'<tr><td>'+mbEsc(x.tx_date)+'</td><td>'+mbEsc(x.filed_date)+'</td><td>'+mbEsc(x.politician_name)+'</td><td>'+mbEsc(x.chamber)+
          '</td><td>'+mbEsc(x.party||'')+'</td><td style="color:'+(x.type==='BUY'?'#15803d':'#b91c1c')+';font-weight:600">'+mbEsc(x.type)+
          '</td><td>'+mbEsc(x.amount_range)+'</td><td>'+(x.filing_url?'<a target="_blank" href="'+mbEsc(x.filing_url)+'">filing ↗</a>':'')+'</td></tr>').join('')+'</table>';
    }
  }catch(e){B.innerHTML='Details load from the dashboard — open this report via the dashboard (Morning Brief button) to see them. ('+mbEsc(e.message)+')';}
}
</script>
"""


def render(as_of, ctx, picks, monitor, card, generated, act=None):
    H = []
    a = H.append
    a(f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
      f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
      f"<title>Morning Brief {as_of}</title><style>{CSS}{BADGE_CSS}</style></head><body>")
    a(f"<div class='mast'><div class='wrap'><h1>US Equities — Morning Brief</h1>"
      f"<div class='sub'>Session of {datetime.strptime(as_of, '%Y-%m-%d'):%A %d %B %Y} · "
      f"generated {generated:%d %b %Y %H:%M} · "
      f"<a href='/reports' style='color:#f0d78c'>archive</a> · "
      f"<a href='/' style='color:#f0d78c'>dashboard</a></div></div></div><div class='wrap'>")

    # ── market ──
    h = ctx.get("health") or {}
    color = {"red": "red", "green": "green"}.get(h.get("color"), "amber")
    a("<h2>1. Market Backdrop</h2>")
    b = ctx.get("breadth", {})
    regime_txt = {
        "correction": "Distribution is elevated on both indices. Treat new breakouts with caution, keep "
                      "initial positions small and demand volume confirmation.",
        "caution": "Distribution is building. Be selective; prefer leaders with tight setups.",
        "healthy": "Distribution is contained — conditions favour adding exposure to leaders on proper buy points.",
        "uptrend_confirmed": "A follow-through day has confirmed a new uptrend — historically the "
                             "best window for new breakouts.",
    }.get(h.get("status"), "")
    a(f"<div class='regime {color}'><b>Market regime: {_e(h.get('label') or 'n/a')}</b>"
      f" &nbsp;<span class='mut'>Distribution days — Nasdaq {ctx.get('dd', {}).get('^IXIC', '—')}, "
      f"S&amp;P 500 {ctx.get('dd', {}).get('^GSPC', '—')}</span><br>{_e(regime_txt)}</div>")
    a("<div class='grid' style='margin-top:12px'>")
    names = {"^GSPC": "S&P 500", "^IXIC": "Nasdaq Comp.", "^RUT": "Russell 2000", "^VIX": "VIX"}
    for sym, nm in names.items():
        d = ctx.get("index", {}).get(sym)
        if not d:
            continue
        a(f"<div class='kpi'><div class='l'>{nm}</div><div class='v'>{d['close']:,.2f}</div>"
          f"<div><span class='{_cls(d['d1'])}'>{d['d1']:+.2%}</span> 1D · "
          f"<span class='{_cls(d['d5'])}'>{d['d5']:+.2%}</span> 1W · "
          f"<span class='{_cls(d['d21'])}'>{d['d21']:+.2%}</span> 1M</div></div>")
    if b.get("n"):
        a(f"<div class='kpi'><div class='l'>Breadth ({b['n']} stocks)</div>"
          f"<div class='v'>{b['above50']:.0%} <span class='mut' style='font-size:13px'>&gt; MA50</span></div>"
          f"<div>{b['above200']:.0%} &gt; MA200 · {b['highs']} near highs / {b['lows']} near lows · "
          f"{b['tt7']} pass Trend Template ≥7</div></div>")
    a("</div>")

    # ── summary table ──
    a(f"<h2>2. Today's {len(picks)} Ideas — Summary</h2><div class='card tw'><table><tr>"
      "<th>#</th><th>Ticker</th><th>Company / Sector</th><th class='r'>Close</th><th class='r'>1D</th>"
      "<th class='r'>1M</th><th class='r'>RS</th><th class='r'>Score</th><th>Thesis</th><th>Signals</th></tr>")
    for i, p in enumerate(picks, 1):
        m = p["m"]
        sector = p["fund"].get("sector") or ""
        tags = "".join(f"<span class='tag{' s' if any(k in t for k in ('Insider','Congress','13D')) else ''}'>"
                       f"{_e(t)}</span>" for t in p["tags"][:6])
        a(f"<tr><td>{i}</td><td><a class='toc' href='#{_e(p['ticker'])}'>{_e(p['ticker'])}</a>{_badges(p['ticker'], act)}"
          f"{' <span class=tag>repeat</span>' if p['prev'] else ''}</td>"
          f"<td>{_e(p.get('name') or '')}<br><span class='mut'>{_e(sector)}</span></td>"
          f"<td class='r'>{m['close']:,.2f}</td><td class='r {_cls(m['pct'])}'>{m['pct']:+.2f}%</td>"
          f"<td class='r {_cls(m['ret21'])}'>{_pct(m['ret21'])}</td>"
          f"<td class='r'>{m['rs_rating'] or '—'}</td>"
          f"<td class='r'><span class='grade g{grade(p)}' style='width:22px;height:22px;line-height:22px;"
          f"font-size:11px'>{grade(p)}</span>{p['score']:.0f}</td>"
          f"<td>{_e(p['nar']['headline'])}</td><td>{tags}</td></tr>")
    a("</table></div>")

    # ── per-stock ──
    a("<h2>3. Stock Notes</h2>")
    for i, p in enumerate(picks, 1):
        m, n, f = p["m"], p["nar"], p["fund"]
        lv = n["levels"]
        a(f"<div class='card pick' id='{_e(p['ticker'])}'>")
        a(f"<h3><span class='grade g{grade(p)}'>{grade(p)}</span>{i}. {_e(p['ticker'])}{_badges(p['ticker'], act)} — "
          f"{_e(p.get('name') or '')} <small>{_e(f.get('sector') or '')}"
          f"{' · ' + _e(f.get('industry')) if f.get('industry') else ''}</small></h3>")
        a(f"<div style='margin:6px 0 2px;font-weight:600'>{_e(n['headline'])}</div>")
        a(f"<div class='mut'>Score {p['score']:.0f} (technical {p['tech']:.0f} · smart money {p['smart']:.0f})"
          f"{' · selected for smart-money activity' if p.get('slot') == 'smart' else ''}</div>")
        a(f"<img class='chart' alt='{_e(p['ticker'])} chart' src='data:image/png;base64,{p['chart']}'>")
        a("<div class='cols'><div>")
        a("<div class='sec-t'>Technical picture</div><ul>" + "".join(f"<li>{_e(x)}</li>" for x in n["tech"]) + "</ul>")
        a("<div class='sec-t'>Smart money</div><ul>" + "".join(f"<li>{_e(x)}</li>" for x in n["smart"]) + "</ul>")
        if n["street"]:
            a("<div class='sec-t'>Fundamentals &amp; Street view</div><ul><li>" + _e("; ".join(n["street"])) + ".</li></ul>")
        a(f"<div class='sec-t'>Catalyst</div><ul><li>{_e(n['catalyst'])}</li></ul>")
        if n["history"]:
            a(f"<div class='sec-t'>vs. previous analysis</div><ul><li>{_e(n['history'])}</li></ul>")
        a("</div><div>")
        s1, r1 = lv["s1"], lv["r1"]
        a("<div class='sec-t'>Key levels</div><div class='lv'>"
          f"<div>Last <b>{m['close']:,.2f}</b></div>"
          f"<div>Support <b>{s1['price']:,.2f}</b>{' ✓' if s1 and s1.get('confirmed') else ''}</div>" if s1 else
          "<div class='sec-t'>Key levels</div><div class='lv'>" f"<div>Last <b>{m['close']:,.2f}</b></div>")
        if r1:
            a(f"<div>Resistance <b>{r1['price']:,.2f}</b></div>")
        if m["ma50"]:
            a(f"<div>MA50 <b>{m['ma50']:,.2f}</b> ({_pct(m['dist50'])})</div>")
        a(f"<div>Invalidation <b class='neg'>{lv['stop']:,.2f}</b> ({_pct(lv['risk_pct'])})</div></div>")
        a("<div class='sec-t'>Risks</div><ul>" +
          ("".join(f"<li>{_e(x)}</li>" for x in n["risks"]) or "<li>No specific red flags from the data.</li>")
          + "</ul>")
        if f.get("next_earnings"):
            a(f"<div class='mut' style='font-size:12px'>Next earnings: {_e(f['next_earnings'])}</div>")
        a("<div class='sec-t'>News (past 7 days)</div><ul class='news'>")
        if p["news"]:
            for x in p["news"]:
                tg = "".join(f"<span class='tag{' w' if t in ('Downgrade','Legal') else ''}'>{t}</span>" for t in x["tags"])
                a(f"<li>{tg}<a href='{_e(x['link'])}' target='_blank' rel='noopener'>{_e(x['title'])}</a> "
                  f"<span class='mut'>— {_e(x['source'])}{', ' + x['date'].strftime('%d %b') if x['date'] else ''}"
                  f"</span></li>")
        else:
            a("<li class='mut'>No headlines found.</li>")
        a("</ul></div></div></div>")

    # ── smart money monitor ──
    a("<h2>4. Smart-Money Monitor (market-wide)</h2><div class='cols'><div>")
    a("<div class='card tw'><div class='sec-t'>Insider open-market purchases — filed last 7 days</div><table>"
      "<tr><th>Ticker</th><th>Company</th><th class='r'>Insiders</th><th class='r'>Value</th><th>Last trade</th></tr>")
    for r in monitor["ins_buys"]:
        a(f"<tr><td><b>{_e(r['ticker'])}</b>{_badges(r['ticker'], act)}</td><td>{_e((r['company'] or '')[:28])}</td>"
          f"<td class='r'>{r['n']}</td><td class='r'>{_money(r['v'])}</td><td>{_e(r['last'])}</td></tr>")
    if not monitor["ins_buys"]:
        a("<tr><td colspan=5 class='mut'>None.</td></tr>")
    a("</table></div>")
    a("<div class='card tw'><div class='sec-t'>New 13D stakes (activist / control) — last 7 days</div><table>"
      "<tr><th>Ticker</th><th>Company</th><th>Filer</th><th class='r'>% class</th><th>Filed</th></tr>")
    for r in monitor["d13"]:
        a(f"<tr><td><b>{_e(r['ticker'] or '—')}</b>{_badges(r['ticker'], act)}</td><td>{_e((r['company_name'] or '')[:26])}</td>"
          f"<td>{_e((r['filer_name'] or '')[:28])}</td><td class='r'>{r['percent_of_class'] or '—'}</td>"
          f"<td>{_e(r['filed_date'])}</td></tr>")
    if not monitor["d13"]:
        a("<tr><td colspan=5 class='mut'>None.</td></tr>")
    a("</table></div></div><div>")
    a("<div class='card tw'><div class='sec-t'>Congress / executive branch — disclosed last 7 days</div><table>"
      "<tr><th>Ticker</th><th>Side</th><th class='r'>Officials</th><th>Who</th></tr>")
    for r in monitor["congress"]:
        side = "<span class='pos'>BUY</span>" if r["type"] == "BUY" else f"<span class='neg'>{_e(r['type'])}</span>"
        a(f"<tr><td><b>{_e(r['ticker'])}</b>{_badges(r['ticker'], act)}</td><td>{side}</td><td class='r'>{r['pols']}</td>"
          f"<td>{_e((r['names'] or '')[:60])}</td></tr>")
    if not monitor["congress"]:
        a("<tr><td colspan=4 class='mut'>None.</td></tr>")
    a("</table></div>")
    a("<div class='card tw'><div class='sec-t'>Largest Form 144 planned sales — last 3 days</div><table>"
      "<tr><th>Ticker</th><th>Seller</th><th>Role</th><th class='r'>Value</th><th>10b5-1</th></tr>")
    for r in monitor["f144"]:
        a(f"<tr><td><b>{_e(r['ticker'] or '—')}</b>{_badges(r['ticker'], act)}</td><td>{_e((r['seller_name'] or '')[:26])}</td>"
          f"<td>{_e(r['relationship'] or '')}</td><td class='r'>{_money(r['market_value'])}</td>"
          f"<td>{'yes' if r['plan_10b51'] else ''}</td></tr>")
    if not monitor["f144"]:
        a("<tr><td colspan=5 class='mut'>None.</td></tr>")
    a("</table></div></div></div>")

    # ── scorecard ──
    a("<h2>5. Track Record — Previous Ideas</h2>")
    if card:
        rets = [r["ret"] for r in card if r["ret"] is not None]
        ex = [r["ret"] - r["spx"] for r in card if r["ret"] is not None and r["spx"] is not None]
        hit = sum(1 for x in rets if x > 0) / len(rets) if rets else 0
        a(f"<div class='grid'><div class='kpi'><div class='l'>Ideas tracked</div><div class='v'>{len(card)}</div></div>"
          f"<div class='kpi'><div class='l'>Average return</div><div class='v {_cls(sum(rets)/len(rets) if rets else 0)}'>"
          f"{_pct(sum(rets)/len(rets) if rets else None)}</div></div>"
          f"<div class='kpi'><div class='l'>Avg vs S&amp;P 500</div><div class='v {_cls(sum(ex)/len(ex) if ex else 0)}'>"
          f"{_pct(sum(ex)/len(ex) if ex else None)}</div></div>"
          f"<div class='kpi'><div class='l'>Hit rate</div><div class='v'>{hit:.0%}</div></div></div>")
        a("<div class='card tw'><table><tr><th>Report</th><th>#</th><th>Ticker</th><th class='r'>Then</th>"
          "<th class='r'>Now</th><th class='r'>Return</th><th class='r'>S&amp;P</th><th class='r'>Excess</th></tr>")
        for r in card:
            exr = r["ret"] - r["spx"] if r["ret"] is not None and r["spx"] is not None else None
            a(f"<tr><td>{_e(r['as_of'])}</td><td>{r['rank']}</td><td><b>{_e(r['ticker'])}</b>{_badges(r['ticker'], act)}</td>"
              f"<td class='r'>{r['close']:,.2f}</td><td class='r'>{(r['now'] or 0):,.2f}</td>"
              f"<td class='r {_cls(r['ret'])}'>{_pct(r['ret'])}</td><td class='r'>{_pct(r['spx'])}</td>"
              f"<td class='r {_cls(exr)}'>{_pct(exr)}</td></tr>")
        a("</table></div>")
    else:
        a("<div class='card mut'>First report — the track record builds from tomorrow.</div>")

    a("<h2>6. Methodology</h2><div class='card' style='font-size:13px'>"
      "Universe: tracked Ticker List + S&amp;P 500 + Russell 1000, price ≥ $5 and ≥ $10M average daily value. "
      "<b>Technical score</b>: Trend Template (×3), RS Rating above 50 (up to 15), RS leader, 30-day breakout, "
      "52-week-high proximity, up-day volume vs 10-day average, pattern scanners (VCP, Cup &amp; Handle, MA200 "
      "breakout, Volume Surge, Momentum, Golden Cross, Pullback, fresh MA Squeeze; capped at 15) and Surge-strategy "
      "signals; minus over-extension (&gt;25% above MA50, RSI &gt; 80). <b>Smart-money score</b>: open-market insider "
      "purchases (SEC Form 4 code P, 30 days), Congress / executive-branch purchases (filed 45 days, traded within 120), new 13D "
      "stakes (30 days); minus heavy insider selling and Form 144 planned sales. Seven ideas are the best overall "
      "scores, three are reserved for the strongest smart-money names that also trade above their 200-day MA. "
      "Grades: A ≥ 60, B ≥ 45, C below. Key levels come from the Volume Profile / pivot S/R module; the "
      "invalidation level sits just under the nearest support (max ~8–10% risk). News: Google News, last 7 days; "
      "fundamentals and analyst targets: Yahoo Finance.</div>")
    a("<div class='disc'>Generated automatically from the dashboard's data for personal research. Not investment "
      "advice. Insider, Congressional and 13D/144 disclosures are filed with a delay (Congress up to 45 days) and "
      "may not reflect current holdings. Verify prices and news before acting.</div>")
    a("</div>" + BADGE_JS + "</body></html>")
    return "".join(H)


# ═════════════════════════════════════════════════════════════════════════════
# Orchestration
# ═════════════════════════════════════════════════════════════════════════════

def _wait_for_lock(lock_path, name, max_minutes):
    t0 = time.time()
    while os.path.exists(lock_path) and time.time() - t0 < max_minutes * 60:
        try:
            from nightly_collect import _pid_alive
            pid = int(open(lock_path).read().strip() or 0)
            if not _pid_alive(pid):
                return
        except Exception:
            return
        log.info("Waiting for %s (PID %s) to finish ...", name, pid)
        time.sleep(60)


def wait_for_collector(max_minutes=90):
    _wait_for_lock(COLLECT_LOCK, "nightly_collect", max_minutes)


def scan_task_done(within_hours=6):
    """as_of of a successful nightly_scan.py run in the last few hours, else None."""
    with get_connection() as conn:
        r = conn.execute("SELECT started_at, detail FROM data_collection_runs WHERE job='pattern_scan' "
                         "AND status='ok' ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    try:
        if (datetime.now() - datetime.fromisoformat(r[0])).total_seconds() > within_hours * 3600:
            return None
        return json.loads(r[1]).get("as_of")
    except Exception:
        return None


def refresh_data(log_fn):
    import fetch_data
    log_fn("Quick US price update ...")
    fetch_data.quick_update(progress_cb=lambda m: None)
    try:
        import surge_strategy
        surge_strategy.scan_signals()
        log_fn("Surge scan done.")
    except Exception as exc:
        log.warning("surge scan: %s", exc)


def run(as_of=None, update=True, force=False):
    setup_database()
    os.makedirs(REPORT_DIR, exist_ok=True)
    scanned = None
    if update:
        wait_for_collector()
        _wait_for_lock(SCAN_LOCK, "nightly_scan", 120)
        scanned = scan_task_done()
        if scanned:
            log.info("nightly_scan already updated prices + scanned %s — reusing it.", scanned)
        else:
            try:
                refresh_data(log.info)
            except Exception as exc:
                log.exception("price update failed (continuing with stored data): %s", exc)
    as_of = as_of or latest_complete_date()
    out_path = os.path.join(REPORT_DIR, f"{as_of}.html")
    if os.path.exists(out_path) and not force:
        log.info("Report for %s already exists — skipping (use --force).", as_of)
        return out_path
    log.info("Building brief for session %s", as_of)

    with get_connection() as conn:
        have_scan = conn.execute("SELECT 1 FROM pattern_scan_results WHERE scan_date=? LIMIT 1",
                                 (as_of,)).fetchone()
    if not have_scan or (update and scanned != as_of):
        try:
            import scan_patterns
            scan_patterns.scan_all_patterns(as_of)
            log.info("Pattern scan for %s done.", as_of)
        except Exception as exc:
            log.warning("pattern scan: %s", exc)

    by, _tracked = load_prices(as_of)
    by = {t: rows for t, rows in by.items() if rows and rows[-1]["date"] == as_of}
    rs, pats, surge = load_rs(as_of), load_patterns(as_of), load_surge()
    sm = load_smart_money(as_of)
    scored = [s for t, rows in by.items()
              if (s := score_ticker(t, rows, rs.get(t), pats.get(t, []), surge.get(t), sm.get(t)))]
    log.info("Scored %d liquid stocks.", len(scored))
    picks = select_picks(scored)
    ctx = market_context(by, as_of)
    prev = previous_appearances([p["ticker"] for p in picks], as_of)

    import volume_profile
    for p in picks:
        t = p["ticker"]
        p["fund"] = fetch_fundamentals(t)
        p["name"] = p["fund"].get("shortName") or p["fund"].get("longName") or t
        p["news"] = fetch_news(t, p["name"])
        smt = sm.get(t) or {}
        p["_d13"] = smt.get("d13", [])
        p["prev"] = prev.get(t, [])
        try:
            vp = volume_profile.analyze(t, by[t][-200:])
            # volume levels first (confirmed ones flagged), then strong pivot S/R
            levels = [{"price": l["price"], "confirmed": bool(l.get("confirmed_by_pivot"))}
                      for l in sorted(vp.get("volume_levels", []),
                                      key=lambda l: (not l.get("confirmed_by_pivot"), -l["volume"]))]
            for pl in vp.get("pivot_levels", [])[:4]:
                if all(abs(pl["price"] / l["price"] - 1) > 0.015 for l in levels):
                    levels.append({"price": pl["price"], "confirmed": False})
        except Exception as exc:
            log.warning("volume profile %s: %s", t, exc)
            levels = []
        for l in levels:
            l["kind"] = "support" if l["price"] <= p["m"]["close"] else "resistance"
        p["levels"] = levels
        p["nar"] = build_narrative(p, p["fund"], p["news"], levels, p["prev"], ctx)
        p["headline"] = p["nar"]["headline"]
        rs_line = load_rs_line(t, by[t][-CHART_DAYS]["date"] if len(by[t]) >= CHART_DAYS else by[t][0]["date"])
        p["chart"] = make_chart(p, by[t], levels, smt, rs_line)
        log.info("  %s done (score %.0f)", t, p["score"])

    card = scorecard(by, ctx, as_of)
    monitor = smart_money_monitor(as_of)
    page = render(as_of, ctx, picks, monitor, card, datetime.now(), load_activity(as_of))
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    save_picks(as_of, picks)
    with get_connection() as conn:
        conn.execute("INSERT INTO data_collection_runs (started_at, finished_at, job, status, detail) "
                     "VALUES (?,?,?,?,?)",
                     (datetime.now().isoformat(timespec="seconds"), datetime.now().isoformat(timespec="seconds"),
                      "daily_brief", "ok", json.dumps({"as_of": as_of, "picks": [p["ticker"] for p in picks]})))
        conn.commit()
    log.info("Brief written: %s (%.0f KB)", out_path, os.path.getsize(out_path) / 1024)
    return out_path


def main():
    ap = argparse.ArgumentParser(description="Nightly Morning Brief (10 stock ideas)")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--no-update", action="store_true", help="skip the price refresh / scans")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    os.makedirs(LOG_DIR, exist_ok=True)
    handlers = [logging.FileHandler(os.path.join(LOG_DIR, "daily_brief.log"), encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=handlers)
    try:
        run(args.as_of, update=not args.no_update, force=args.force)
    except Exception:
        log.exception("Daily brief failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
