"""Trade Journal + mandatory Pre-Trade Check.

One `journal_trades` row per trade idea / position (the plan, the Pre-Trade
Check answers, the warnings that fired, the Post-Trade Review) and one
`journal_fills` row per execution (BUY / SELL). Realized P/L, return % and
holding days are always computed from the fills (FIFO), never typed in.

Workflow (status):
    cooling   news-driven plan, locked for CONFIG["NEWS_COOLDOWN_MIN"] minutes
    planned   check passed, ready -> record the BUY fill(s) when filled
    open      net shares > 0
    closed    net shares back to 0 -> Post-Trade Review required
    reviewed  review done
    cancelled plan abandoned / never filled ("入唔到貨都接受")

The check is enforced HERE (server side): `create_trade()` refuses a plan with
missing answers, or with red warnings that are not acknowledged (risk_ack).
The page only shows what this module returns. Note the limit: the journal
cannot stop an order placed directly at the broker.

Pure functions (`analyze_support`, `detect_chase`, `events_in_window`,
`evaluate`, `fifo_pnl`, `compute_stats`) are tested by scripts/test_journal.py.
"""

import json
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from db_setup import get_connection

NY = ZoneInfo("America/New_York")

# ─── CONFIG (edit here) ───────────────────────────────────────────────────────
CONFIG = {
    "RR_MIN": 2.0,                 # target/stop reward:risk below this -> red warning
    "MAX_DIST_SUPPORT_PCT": 5.0,   # entry more than this % above support -> red warning
    "EVENT_WINDOW_TDAYS": 5,       # CPI / FOMC / other events within N trading days -> red
    "EARNINGS_WINDOW_TDAYS": 10,   # earnings within N trading days -> red (gaps dominate a swing trade)
    "MAX_RISK_PCT": 2.0,           # max loss above this % of capital -> red
    "MAX_POSITION_PCT": 25.0,      # position above this % of capital -> red
    "CHASE_NEAR_HIGH_PCT": 1.0,    # entry within this % of the day's high = chasing
    "CHASE_BOUNCE_PCT": 3.0,       # entry this % or more above the day's low = chasing a bounce
    "SUPPORT_TEST_PCT": 1.5,       # a low within this % above support counts as a test
    "SUPPORT_LOOKBACK_BARS": 10,   # bars checked for the test + stabilization signs
    "NEWS_COOLDOWN_MIN": 30,       # news-driven plans can only be confirmed after this
    "THESIS_MIN_CHARS": 12,        # after removing vague phrases
    "VAGUE_PHRASES": ["博反彈", "搏反彈", "反彈", "應該會升", "估佢升", "感覺", "睇好",
                      "rebound", "bounce", "should go up", "feels", "gut"],
    "CONTEXT_TTL_SEC": 300,
}

SETUP_TYPES = {"breakout": "突破", "pullback": "回踩支持", "event": "事件博弈", "other": "其他"}
SUPPORT_SOURCES = {"prior_low": "前低", "ma": "均線", "volume": "成交密集區"}
STAB_SIGNS = {"long_lower_shadow": "長下影線", "reclaim_high": "重奪日內高位", "higher_low": "Higher low"}
ERROR_TAGS = {"chase": "追價", "no_stop": "無止蝕", "knife": "接刀", "ignored_events": "無視事件日曆",
              "emotional_sell": "情緒賣出", "oversized": "倉位過大", "impatient": "冇耐性等價", "other": "其他"}
ORDER_TYPES = ["Limit", "Limit GTC", "Market", "Stop", "Stop Limit"]
EVENT_TYPES = {"earnings": "業績", "cpi": "CPI", "fomc": "FOMC", "other": "其他"}

# Default macro calendar (editable in the page's Settings; stored in journal_settings).
# VERIFY against bls.gov/schedule/news_release/cpi.htm and federalreserve.gov/monetarypolicy/fomccalendars.htm
DEFAULT_MACRO_EVENTS = [
    {"date": "2026-11-10", "type": "cpi", "label": "CPI (Oct)"},
    {"date": "2026-12-09", "type": "fomc", "label": "FOMC decision"},
    {"date": "2026-12-10", "type": "cpi", "label": "CPI (Nov)"},
]

DEFAULT_SETTINGS = {"capital": None, "currency": "USD", "fx_hkd": 7.8,
                    "default_commission": 1.0, "macro_events": DEFAULT_MACRO_EVENTS}


# ═════════════════════════════════════════════════════════════════════════════
# Settings
# ═════════════════════════════════════════════════════════════════════════════

def get_settings():
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    with get_connection() as conn:
        for r in conn.execute("SELECT key, value FROM journal_settings"):
            try:
                out[r["key"]] = json.loads(r["value"])
            except (TypeError, ValueError):
                out[r["key"]] = r["value"]
    return out


def save_settings(data):
    allowed = {"capital", "currency", "fx_hkd", "default_commission", "macro_events"}
    clean = {}
    for k, v in (data or {}).items():
        if k not in allowed:
            continue
        if k in ("capital", "fx_hkd", "default_commission"):
            v = _num(v)
            if v is not None and v < 0:
                raise ValueError(f"{k} must be >= 0")
        if k == "macro_events":
            evs = []
            for e in v or []:
                d = _date(e.get("date"))
                if not d:
                    continue
                t = e.get("type") if e.get("type") in EVENT_TYPES else "other"
                evs.append({"date": d, "type": t, "label": str(e.get("label") or EVENT_TYPES[t])[:60]})
            v = sorted(evs, key=lambda e: e["date"])
        clean[k] = v
    with get_connection() as conn:
        for k, v in clean.items():
            conn.execute("INSERT INTO journal_settings(key, value) VALUES(?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, json.dumps(v)))
    return get_settings()


# ═════════════════════════════════════════════════════════════════════════════
# Small helpers
# ═════════════════════════════════════════════════════════════════════════════

def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _date(v):
    if not v:
        return None
    s = str(v).strip()[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _yn(v):
    """Tri-state answer: True / False / None (unanswered)."""
    if v in (True, 1, "1", "yes", "true", "是", "Y", "y"):
        return True
    if v in (False, 0, "0", "no", "false", "否", "N", "n"):
        return False
    return None


def _now_et():
    return datetime.now(NY)


def trading_days_between(d0, d1):
    """Weekdays strictly after d0 up to and including d1 (0 if d1 <= d0).
    Holidays are ignored (close enough for a 5-10 day window)."""
    a = datetime.strptime(d0, "%Y-%m-%d").date()
    b = datetime.strptime(d1, "%Y-%m-%d").date()
    if b <= a:
        return 0
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


# ═════════════════════════════════════════════════════════════════════════════
# Pure analysis
# ═════════════════════════════════════════════════════════════════════════════

def analyze_support(bars, support, cfg=None):
    """Has price tested `support` in the last N bars, and did it show signs of
    stabilizing afterwards? bars: ascending dicts with date/open/high/low/close.
    Returns {tested, test_date, broken, signs: {code: date|None}, stabilized}.
    Signs (daily-bar approximations):
      long_lower_shadow  a test bar whose lower shadow >= 2x body and >= half the
                         range, closing back at/above support
      reclaim_high       after the test, a close above the PREVIOUS bar's high
      higher_low         the lowest low since the test is >= 2 bars old and every
                         later low is higher"""
    cfg = {**CONFIG, **(cfg or {})}
    out = {"tested": False, "test_date": None, "broken": False,
           "signs": {k: None for k in STAB_SIGNS}, "stabilized": False}
    if not bars or not support:
        return out
    w = [b for b in bars if b.get("low") is not None and b.get("close") is not None][-cfg["SUPPORT_LOOKBACK_BARS"]:]
    if not w:
        return out
    lim = support * (1 + cfg["SUPPORT_TEST_PCT"] / 100)
    t = next((i for i, b in enumerate(w) if b["low"] <= lim), None)
    out["broken"] = w[-1]["close"] < support * 0.99
    if t is None:
        return out
    out["tested"] = True
    out["test_date"] = w[t]["date"]
    for i in range(t, len(w)):
        b = w[i]
        o = b.get("open") if b.get("open") is not None else b["close"]
        body = abs(b["close"] - o)
        rng = (b.get("high") or max(o, b["close"])) - b["low"]
        shadow = min(o, b["close"]) - b["low"]
        if (out["signs"]["long_lower_shadow"] is None and b["low"] <= lim and rng > 0
                and shadow >= 2 * body and shadow >= 0.5 * rng and b["close"] >= support):
            out["signs"]["long_lower_shadow"] = b["date"]
        if (out["signs"]["reclaim_high"] is None and i > t and w[i - 1].get("high") is not None
                and b["close"] > w[i - 1]["high"]):
            out["signs"]["reclaim_high"] = b["date"]
    seg = w[t:]
    m = min(range(len(seg)), key=lambda i: seg[i]["low"])
    if m <= len(seg) - 3 and all(x["low"] > seg[m]["low"] for x in seg[m + 1:]):
        out["signs"]["higher_low"] = seg[-1]["date"]
    out["stabilized"] = any(out["signs"].values()) and not out["broken"]
    return out


def detect_chase(entry, day_high, day_low, cfg=None):
    """Entry near the day's high, or well above the day's low after a bounce."""
    cfg = {**CONFIG, **(cfg or {})}
    reasons = []
    if entry and day_high and entry >= day_high * (1 - cfg["CHASE_NEAR_HIGH_PCT"] / 100):
        reasons.append(f"入場價 {entry:.2f} 距當日高位 {day_high:.2f} 只差 {(day_high - entry) / day_high * 100:.1f}%")
    if entry and day_low and entry >= day_low * (1 + cfg["CHASE_BOUNCE_PCT"] / 100):
        reasons.append(f"由當日低位 {day_low:.2f} 反彈咗 {(entry / day_low - 1) * 100:.1f}% 先入")
    return {"chasing": bool(reasons), "reasons": reasons}


def events_in_window(today, earnings_date, macro_events, extra_events=None, cfg=None):
    """Events in the coming window. Returns (flagged, upcoming): flagged = those
    inside the red window (earnings: EARNINGS_WINDOW_TDAYS, others:
    EVENT_WINDOW_TDAYS, today counts as 0); upcoming = everything in 15 trading days."""
    cfg = {**CONFIG, **(cfg or {})}
    evs = []
    if earnings_date:
        evs.append({"date": earnings_date, "type": "earnings", "label": "業績 Earnings"})
    evs += [dict(e) for e in (macro_events or [])]
    evs += [dict(e) for e in (extra_events or []) if e.get("date")]
    flagged, upcoming = [], []
    for e in sorted(evs, key=lambda e: e["date"]):
        if e["date"] < today:
            continue
        n = trading_days_between(today, e["date"])
        e["tdays"] = n
        if n <= 15:
            upcoming.append(e)
        lim = cfg["EARNINGS_WINDOW_TDAYS"] if e.get("type") == "earnings" else cfg["EVENT_WINDOW_TDAYS"]
        if n <= lim:
            flagged.append(e)
    return flagged, upcoming


def _vague(thesis):
    s = thesis or ""
    for p in CONFIG["VAGUE_PHRASES"]:
        s = s.replace(p, "")
        s = s.replace(p.capitalize(), "")
    s = "".join(ch for ch in s if ch.isalnum())
    return len(s) < CONFIG["THESIS_MIN_CHARS"]


def evaluate(form, ctx, settings, today=None, cfg=None):
    """The Pre-Trade Check. form: the page's answers. ctx: get_context() output
    (may be {} when no market data). Returns {errors, warnings, computed}.
    errors block saving; warnings (red) need risk_ack."""
    cfg = {**CONFIG, **(cfg or {})}
    ctx = ctx or {}
    today = today or _now_et().strftime("%Y-%m-%d")
    E, W, C = [], [], {}

    def err(field, msg):
        E.append({"field": field, "msg": msg})

    def warn(code, msg):
        W.append({"code": code, "msg": msg})

    ticker = (form.get("ticker") or "").strip().upper()
    if not ticker:
        err("ticker", "請輸入 ticker")
    entry = _num(form.get("entry_price"))
    shares = _num(form.get("shares"))
    support = _num(form.get("support_price"))
    stop = _num(form.get("stop_price"))
    target = _num(form.get("target_price"))
    capital = _num(settings.get("capital"))
    commission = _num(form.get("commission"))
    if commission is None:
        commission = _num(settings.get("default_commission")) or 0.0

    # 1 thesis
    thesis = (form.get("thesis") or "").strip()
    if not thesis:
        err("thesis", "1. 入場理由必填")
    elif _vague(thesis):
        err("thesis", "1. 入場理由太空泛 — 講清楚點解係呢個價、呢個時間（唔可以只寫「博反彈」）")
    if form.get("setup_type") not in SETUP_TYPES:
        err("setup_type", "1. 揀 setup 類型")
    elif form.get("setup_type") == "other" and not (form.get("setup_other") or "").strip():
        err("setup_other", "1. setup 揀「其他」要寫明係乜")

    # entry / position basics
    if not entry or entry <= 0:
        err("entry_price", "入場價（計劃價）必填")
    if not shares or shares <= 0:
        err("shares", "6. 股數必填")
    if form.get("order_type") not in ORDER_TYPES:
        err("order_type", "揀訂單類型")

    # 2 support
    if not support or support <= 0:
        err("support_price", "2. 支持位價格必填")
    elif entry and support >= entry:
        err("support_price", "2. 支持位要低過入場價")
    if form.get("support_source") not in SUPPORT_SOURCES:
        err("support_source", "2. 揀支持位來源（前低／均線／成交密集區）")
    if entry and support and support < entry:
        dist = (entry - support) / entry * 100
        C["dist_support_pct"] = round(dist, 2)
        if dist > cfg["MAX_DIST_SUPPORT_PCT"]:
            warn("far_from_support", f"入場價高過支持位 {dist:.1f}%（> {cfg['MAX_DIST_SUPPORT_PCT']:.0f}%）— 考慮掛單喺支持位附近等")

    # 3 support confirmation
    tested = _yn(form.get("support_tested"))
    signs = [s for s in (form.get("stab_signs") or []) if s in STAB_SIGNS]
    if tested is None:
        err("support_tested", "3. 價格有冇測試過支持位？")
    if support and ctx.get("bars"):
        C["support_auto"] = analyze_support(ctx["bars"], support, cfg)
    if tested is False or not signs:
        warn("knife", "3. 未見測試支持位後企穩（長下影線／重奪日內高位／higher low）— 接刀風險")
    if C.get("support_auto", {}).get("broken"):
        warn("support_broken", "3. 最新收市已跌穿支持位")

    # 4 stop
    if not stop or stop <= 0:
        err("stop_price", "4. 止蝕位必填 — 跌穿幾多你一定走？")
    elif entry and stop >= entry:
        err("stop_price", "4. 止蝕位要低過入場價")

    # 5 target + R:R
    if not target or target <= 0:
        err("target_price", "5. 目標位必填")
    elif entry and target <= entry:
        err("target_price", "5. 目標位要高過入場價")
    if entry and stop and target and stop < entry < target:
        rr = (target - entry) / (entry - stop)
        C["rr"] = round(rr, 2)
        if rr < cfg["RR_MIN"]:
            warn("low_rr", f"5. 風險回報比 {rr:.2f} : 1，低過 {cfg['RR_MIN']:.0f} : 1")

    # 6 size
    if not capital:
        err("capital", "6. 未設定總資金 — 喺 Settings 填咗先")
    if entry and shares:
        C["position_value"] = round(entry * shares, 2)
        if stop and stop < entry:
            C["max_loss"] = round((entry - stop) * shares + 2 * commission, 2)
        if capital:
            C["position_pct"] = round(entry * shares / capital * 100, 2)
            if C["position_pct"] > cfg["MAX_POSITION_PCT"]:
                warn("oversized", f"6. 倉位佔總資金 {C['position_pct']:.1f}%（> {cfg['MAX_POSITION_PCT']:.0f}%）")
            if "max_loss" in C:
                C["max_loss_pct"] = round(C["max_loss"] / capital * 100, 2)
                if C["max_loss_pct"] > cfg["MAX_RISK_PCT"]:
                    warn("risk_too_big", f"6. 最大虧損 ${C['max_loss']:,.2f} = 總資金 {C['max_loss_pct']:.1f}%（> {cfg['MAX_RISK_PCT']:.0f}%）")

    # 7 events
    earnings = _date(form.get("earnings_date")) or ctx.get("earnings_date")
    extra = [{"date": _date(e.get("date")), "type": "other", "label": (e.get("label") or "其他事件")[:60]}
             for e in (form.get("other_events") or []) if _date(e.get("date"))]
    flagged, upcoming = events_in_window(today, earnings, settings.get("macro_events"), extra, cfg)
    C["events_flagged"], C["events_upcoming"] = flagged, upcoming
    if not form.get("events_checked"):
        err("events_checked", "7. 剔「我已檢查未來 5 日事件日曆」")
    for e in flagged:
        when = "今日" if e["tdays"] == 0 else f"{e['tdays']} 個交易日後"
        warn("event_" + e.get("type", "other"), f"7. {e['label']} {e['date']}（{when}）")

    # 8 news
    news = _yn(form.get("news_driven"))
    if news is None:
        err("news_driven", "8. 係咪新聞／事件驅動？")
    elif news:
        dev, stab = _yn(form.get("event_developing")), _yn(form.get("event_stabilized"))
        if dev is None:
            err("event_developing", "8. 事件仲發展緊（調查、訴訟、監管）？")
        if stab is None:
            err("event_stabilized", "8. 價格有冇企穩？")
        if stab is False or (dev and stab is not True):
            warn("news_unstable", "8. 新聞事件未企穩 — 事件仲發展緊就唔好急住入")
        C["cooldown_min"] = cfg["NEWS_COOLDOWN_MIN"]

    # 9 chasing
    chasing = _yn(form.get("chasing"))
    if chasing is None:
        err("chasing", "9. 你係咪喺追價？")
    day_high = _num(form.get("day_high")) or ctx.get("day_high")
    day_low = _num(form.get("day_low")) or ctx.get("day_low")
    ch = detect_chase(entry, day_high, day_low, cfg) if entry and entry >= (day_low or 0) else {"chasing": False, "reasons": []}
    C["chase_auto"] = ch
    if chasing or ch["chasing"]:
        warn("chasing", "9. 追價：" + ("；".join(ch["reasons"]) if ch["reasons"] else "你自己確認係追價"))

    # 10 accept no fill
    if _yn(form.get("accept_no_fill")) is not True:
        err("accept_no_fill", "10. 唔接受入唔到貨，就唔好落單")

    if W and not form.get("risk_ack"):
        err("risk_ack", "有紅色警告 — 要剔「我明白風險」先可以繼續")
    return {"errors": E, "warnings": W, "computed": C}


def fifo_pnl(fills):
    """fills: dicts with side BUY/SELL, filled_at, shares, price, commission (any order).
    FIFO-matches sells against buys. Returns net_shares, avg_cost (open lots incl.
    commission), realized_pl, cost_closed (cost of the shares sold, incl. buy
    commission), realized_pct, first_buy, last_sell, holding_days, errors."""
    lots, realized, cost_closed = [], 0.0, 0.0
    first_buy = last_sell = None
    errors = []
    for f in sorted(fills, key=lambda f: (f["filled_at"] or "", 0 if f["side"] == "BUY" else 1)):
        q, p, c = float(f["shares"]), float(f["price"]), float(f.get("commission") or 0)
        if f["side"] == "BUY":
            lots.append([q, p + c / q])
            first_buy = first_buy or f["filled_at"]
        else:
            left, cost = q, 0.0
            while left > 1e-9 and lots:
                take = min(left, lots[0][0])
                cost += take * lots[0][1]
                lots[0][0] -= take
                left -= take
                if lots[0][0] <= 1e-9:
                    lots.pop(0)
            if left > 1e-9:
                errors.append(f"SELL {q:g} on {f['filled_at']} exceeds shares held")
            sold = q - left
            realized += sold * p - c * (sold / q) - cost
            cost_closed += cost
            last_sell = f["filled_at"]
    net = sum(l[0] for l in lots)
    avg = sum(l[0] * l[1] for l in lots) / net if net > 1e-9 else None
    hold = None
    if first_buy:
        end = last_sell if net <= 1e-9 and last_sell else _now_et().strftime("%Y-%m-%d")
        hold = (datetime.strptime(end[:10], "%Y-%m-%d") - datetime.strptime(first_buy[:10], "%Y-%m-%d")).days
    return {"net_shares": round(net, 6), "avg_cost": avg, "realized_pl": round(realized, 2),
            "cost_closed": round(cost_closed, 2),
            "realized_pct": round(realized / cost_closed * 100, 2) if cost_closed else None,
            "first_buy": first_buy, "last_sell": last_sell, "holding_days": hold, "errors": errors}


def compute_stats(trades, fx_hkd=None):
    """trades: dicts with status, realized_pl, closed_at, followed_plan, error_tags (list).
    Only closed / reviewed trades count."""
    done = [t for t in trades if t.get("status") in ("closed", "reviewed") and t.get("realized_pl") is not None]
    wins = [t["realized_pl"] for t in done if t["realized_pl"] > 0]
    losses = [t["realized_pl"] for t in done if t["realized_pl"] <= 0]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None

    def grp(rows):
        pl = [r["realized_pl"] for r in rows]
        return {"count": len(rows), "wins": sum(1 for x in pl if x > 0),
                "win_rate": round(sum(1 for x in pl if x > 0) / len(pl) * 100, 1) if pl else None,
                "total_pl": round(sum(pl), 2), "avg_pl": round(sum(pl) / len(pl), 2) if pl else None}

    plan = {"followed": grp([t for t in done if t.get("followed_plan") == 1]),
            "not_followed": grp([t for t in done if t.get("followed_plan") == 0]),
            "not_reviewed": grp([t for t in done if t.get("followed_plan") is None])}
    tags = {}
    for t in done:
        for tag in t.get("error_tags") or []:
            g = tags.setdefault(tag, {"tag": tag, "label": ERROR_TAGS.get(tag, tag), "count": 0,
                                      "total_pl": 0.0, "loss": 0.0})
            g["count"] += 1
            g["total_pl"] += t["realized_pl"]
            if t["realized_pl"] < 0:
                g["loss"] += t["realized_pl"]
    tag_rows = sorted(tags.values(), key=lambda g: (g["loss"], -g["count"]))
    for g in tag_rows:
        g["total_pl"], g["loss"] = round(g["total_pl"], 2), round(g["loss"], 2)
    months = {}
    for t in done:
        m = (t.get("closed_at") or "")[:7] or "unknown"
        months.setdefault(m, {"month": m, "pl": 0.0, "count": 0})
        months[m]["pl"] += t["realized_pl"]
        months[m]["count"] += 1
    cum, monthly = 0.0, []
    for m in sorted(months):
        cum += months[m]["pl"]
        monthly.append({**months[m], "pl": round(months[m]["pl"], 2), "cum": round(cum, 2)})
    total = sum(t["realized_pl"] for t in done)
    return {
        "count": len(done), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(done) * 100, 1) if done else None,
        "avg_win": round(avg_win, 2) if avg_win is not None else None,
        "avg_loss": round(avg_loss, 2) if avg_loss is not None else None,
        "payoff_ratio": round(avg_win / abs(avg_loss), 2) if avg_win and avg_loss else None,
        "expectancy": round(total / len(done), 2) if done else None,
        "total_pl": round(total, 2),
        "total_pl_hkd": round(total * fx_hkd, 0) if fx_hkd else None,
        "plan": plan, "tags": tag_rows, "monthly": monthly,
        "open_reviews": sum(1 for t in trades if t.get("status") == "closed"),
    }


# ═════════════════════════════════════════════════════════════════════════════
# Market context (auto-filled answers)
# ═════════════════════════════════════════════════════════════════════════════

_ctx_cache = {}


def _bars_yahoo(ticker):
    import yfinance as yf
    df = yf.Ticker(ticker).history(period="1y", interval="1d", auto_adjust=False)
    if df is None or df.empty:
        return []
    out = []
    for idx, r in df.iterrows():
        if r["Close"] != r["Close"]:
            continue
        out.append({"date": idx.strftime("%Y-%m-%d"), "open": float(r["Open"]), "high": float(r["High"]),
                    "low": float(r["Low"]), "close": float(r["Close"]), "volume": float(r["Volume"] or 0)})
    return out


def _bars_db(ticker):
    with get_connection() as conn:
        for tbl in ("stocks_daily", "universe_prices"):
            rows = conn.execute(f"SELECT date, open, high, low, close, volume FROM {tbl} "
                                f"WHERE ticker = ? AND close IS NOT NULL ORDER BY date DESC LIMIT 260",
                                (ticker,)).fetchall()
            if rows:
                return [dict(r) for r in reversed(rows)]
    return []


def _earnings_date(ticker):
    try:
        import yfinance as yf
        cal = yf.Ticker(ticker).calendar or {}
        ed = cal.get("Earnings Date")
        if ed:
            d = ed[0] if isinstance(ed, (list, tuple)) else ed
            return d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else _date(d)
    except Exception:
        pass
    return None


def _sma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def get_context(ticker, force=False):
    """Market data for the check: last price, today's high/low, MAs, 52-wk range,
    support candidates (前低 / 均線 / 成交密集區), next earnings. Yahoo first
    (~15 min delayed, includes today's forming bar), stored bars as fallback."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {}
    hit = _ctx_cache.get(ticker)
    if hit and not force and time.time() - hit[0] < CONFIG["CONTEXT_TTL_SEC"]:
        return hit[1]
    src, bars = "yahoo", []
    try:
        bars = _bars_yahoo(ticker)
    except Exception:
        bars = []
    if not bars:
        src, bars = "stored", _bars_db(ticker)
    if not bars:
        ctx = {"ticker": ticker, "error": "no price data for this ticker"}
        _ctx_cache[ticker] = (time.time(), ctx)
        return ctx
    last = bars[-1]
    closes = [b["close"] for b in bars]
    price = last["close"]
    now = _now_et()
    ctx = {"ticker": ticker, "source": src, "bar_date": last["date"], "price": price,
           "day_open": last.get("open"), "day_high": last.get("high"), "day_low": last.get("low"),
           "prev_close": bars[-2]["close"] if len(bars) > 1 else None,
           "bar_forming": last["date"] == now.strftime("%Y-%m-%d") and now.weekday() < 5
                          and now.time() < datetime.strptime("16:00", "%H:%M").time(),
           "ma20": _sma(closes, 20), "ma50": _sma(closes, 50), "ma100": _sma(closes, 100), "ma200": _sma(closes, 200),
           "low_52w": min(b["low"] for b in bars[-252:]), "high_52w": max(b["high"] for b in bars[-252:]),
           "bars": bars[-30:], "earnings_date": _earnings_date(ticker)}
    ctx["low_52w_date"] = next(b["date"] for b in bars[-252:] if b["low"] == ctx["low_52w"])

    cands = []
    for n in (20, 50, 100, 200):
        v = ctx[f"ma{n}"]
        if v and v < price:
            cands.append({"price": round(v, 2), "source": "ma", "label": f"MA{n}"})
    cands.append({"price": round(ctx["low_52w"], 2), "source": "prior_low", "label": f"52 週低位 ({ctx['low_52w_date']})"})
    low20 = min(bars[-20:], key=lambda b: b["low"])
    if low20["low"] < price:
        cands.append({"price": round(low20["low"], 2), "source": "prior_low", "label": f"20 日低位 ({low20['date']})"})
    try:
        import volume_profile as vp
        res = vp.analyze(ticker, bars, {"lookback_days": 120})
        for l in res.get("pivot_levels") or []:
            if l["price"] < price:
                cands.append({"price": round(l["price"], 2), "source": "prior_low",
                              "label": f"前低／轉折位 ({l['touches']} 次)"})
        for l in res.get("volume_levels") or []:
            if l["price"] < price:
                tag = " ✓轉折確認" if l.get("confirmed_by_pivot") else ""
                cands.append({"price": round(l["price"], 2), "source": "volume", "label": f"成交密集區{tag}"})
        if res.get("poc") and res["poc"]["price"] < price:
            cands.append({"price": round(res["poc"]["price"], 2), "source": "volume", "label": "成交最密 POC"})
    except Exception:
        pass
    seen, uniq = set(), []
    for c in sorted(cands, key=lambda c: -c["price"]):
        k = round(c["price"], 1)
        if k in seen:
            continue
        seen.add(k)
        c["dist_pct"] = round((price - c["price"]) / price * 100, 2)
        uniq.append(c)
    ctx["support_candidates"] = uniq[:10]
    _ctx_cache[ticker] = (time.time(), ctx)
    return ctx


# ═════════════════════════════════════════════════════════════════════════════
# Persistence
# ═════════════════════════════════════════════════════════════════════════════

_JSON_COLS = ("stab_signs", "events", "warnings", "error_tags", "check_snapshot", "chase_reasons")


def _row(r):
    d = dict(r)
    for k in _JSON_COLS:
        if k in d:
            try:
                d[k] = json.loads(d[k]) if d[k] else ([] if k != "check_snapshot" else {})
            except (TypeError, ValueError):
                pass
    return d


def _recalc(conn, trade_id):
    """Recompute P/L, holding days and status from the fills."""
    t = conn.execute("SELECT status FROM journal_trades WHERE id = ?", (trade_id,)).fetchone()
    fills = [dict(f) for f in conn.execute("SELECT * FROM journal_fills WHERE trade_id = ?", (trade_id,))]
    p = fifo_pnl(fills)
    status = t["status"]
    has_sell = any(f["side"] == "SELL" for f in fills)
    if p["net_shares"] > 1e-9:
        status = "open"
    elif has_sell:
        status = "reviewed" if status == "reviewed" else "closed"
    elif status in ("open", "closed", "reviewed"):
        status = "planned"
    conn.execute("""UPDATE journal_trades SET realized_pl = ?, realized_pl_pct = ?, holding_days = ?,
                    net_shares = ?, avg_cost = ?, opened_at = ?, closed_at = ?, status = ?, updated_at = ?
                    WHERE id = ?""",
                 (p["realized_pl"] if has_sell else None, p["realized_pct"] if has_sell else None,
                  p["holding_days"], p["net_shares"], p["avg_cost"], p["first_buy"],
                  p["last_sell"] if has_sell and p["net_shares"] <= 1e-9 else None, status,
                  _now_et().strftime("%Y-%m-%d %H:%M"), trade_id))
    return p


def create_trade(form, ctx=None, retrospective=False, now=None):
    """Validate (unless retrospective) and store a plan. Returns (trade, result)
    where result is the evaluate() output; trade is None when refused."""
    settings = get_settings()
    now = now or _now_et()
    ctx = ctx if ctx is not None else get_context(form.get("ticker"))
    res = evaluate(form, ctx, settings, today=form.get("planned_date") or now.strftime("%Y-%m-%d"))
    if res["errors"] and not retrospective:
        return None, res
    C = res["computed"]
    news = _yn(form.get("news_driven"))
    status = "cooling" if news and not retrospective else "planned"
    cooldown_until = (now + timedelta(minutes=CONFIG["NEWS_COOLDOWN_MIN"])).strftime("%Y-%m-%d %H:%M") if status == "cooling" else None
    events = [{k: e[k] for k in ("date", "type", "label")} for e in C.get("events_flagged", [])]
    snap = form.get("check_snapshot") or {k: ctx.get(k) for k in
            ("source", "bar_date", "price", "day_open", "day_high", "day_low", "prev_close",
             "ma20", "ma50", "ma200", "low_52w", "high_52w", "earnings_date")}
    if C.get("support_auto"):
        snap["support_auto"] = C["support_auto"]
    entry = _num(form.get("entry_price"))
    vals = {
        "ticker": (form.get("ticker") or "").strip().upper(), "side": "LONG", "status": status,
        "created_at": now.strftime("%Y-%m-%d %H:%M"), "planned_date": _date(form.get("planned_date")) or now.strftime("%Y-%m-%d"),
        "cooldown_until": cooldown_until, "confirmed_at": None if status == "cooling" else now.strftime("%Y-%m-%d %H:%M"),
        "thesis": (form.get("thesis") or "").strip(), "setup_type": form.get("setup_type"),
        "setup_other": form.get("setup_other"),
        "entry_price": entry, "shares": _num(form.get("shares")), "order_type": form.get("order_type"),
        "commission_est": _num(form.get("commission")),
        "price_at_check": ctx.get("price") if not form.get("check_snapshot") else snap.get("price"),
        "day_high": _num(form.get("day_high")) or ctx.get("day_high"),
        "day_low": _num(form.get("day_low")) or ctx.get("day_low"),
        "support_price": _num(form.get("support_price")), "support_source": form.get("support_source"),
        "support_note": form.get("support_note"), "resistance_price": _num(form.get("resistance_price")),
        "dist_support_pct": C.get("dist_support_pct"),
        "support_tested": _yn(form.get("support_tested")),
        "stab_signs": json.dumps([s for s in (form.get("stab_signs") or []) if s in STAB_SIGNS]),
        "stop_price": _num(form.get("stop_price")), "target_price": _num(form.get("target_price")),
        "rr_ratio": C.get("rr"), "position_pct": C.get("position_pct"), "max_loss": C.get("max_loss"),
        "max_loss_pct": C.get("max_loss_pct"), "capital_at_check": _num(settings.get("capital")),
        "events": json.dumps(form.get("events_override") or events, ensure_ascii=False),
        "earnings_date": _date(form.get("earnings_date")) or ctx.get("earnings_date"),
        "news_driven": news, "event_developing": _yn(form.get("event_developing")),
        "event_stabilized": _yn(form.get("event_stabilized")),
        "chasing": _yn(form.get("chasing")), "chase_auto": 1 if C.get("chase_auto", {}).get("chasing") else 0,
        "chase_reasons": json.dumps(C.get("chase_auto", {}).get("reasons", []), ensure_ascii=False),
        "accept_no_fill": _yn(form.get("accept_no_fill")),
        "warnings": json.dumps(res["warnings"], ensure_ascii=False), "risk_ack": 1 if form.get("risk_ack") else 0,
        "pre_check_done": 0 if retrospective else 1,
        "check_snapshot": json.dumps(snap, ensure_ascii=False, default=str),
        "notes": form.get("notes"), "updated_at": now.strftime("%Y-%m-%d %H:%M"),
    }
    cols = ", ".join(vals)
    with get_connection() as conn:
        cur = conn.execute(f"INSERT INTO journal_trades ({cols}) VALUES ({', '.join('?' * len(vals))})",
                           list(vals.values()))
        tid = cur.lastrowid
    return get_trade(tid), res


def confirm_trade(trade_id, now=None):
    now = now or _now_et()
    with get_connection() as conn:
        t = conn.execute("SELECT status, cooldown_until FROM journal_trades WHERE id = ?", (trade_id,)).fetchone()
        if not t:
            raise LookupError("trade not found")
        if t["status"] != "cooling":
            return get_trade(trade_id)
        if t["cooldown_until"] and now.strftime("%Y-%m-%d %H:%M") < t["cooldown_until"]:
            raise PermissionError(f"新聞驅動交易要冷靜到 {t['cooldown_until']} ET 先可以確認")
        conn.execute("UPDATE journal_trades SET status = 'planned', confirmed_at = ?, updated_at = ? WHERE id = ?",
                     (now.strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d %H:%M"), trade_id))
    return get_trade(trade_id)


def add_fill(trade_id, data):
    side = (data.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    shares, price = _num(data.get("shares")), _num(data.get("price"))
    if not shares or shares <= 0 or not price or price <= 0:
        raise ValueError("shares and price must be > 0")
    filled_at = (data.get("filled_at") or _now_et().strftime("%Y-%m-%d %H:%M")).replace("T", " ")[:16]
    if not _date(filled_at):
        raise ValueError("filled_at must be YYYY-MM-DD[ HH:MM]")
    with get_connection() as conn:
        t = conn.execute("SELECT status, net_shares FROM journal_trades WHERE id = ?", (trade_id,)).fetchone()
        if not t:
            raise LookupError("trade not found")
        if side == "BUY" and t["status"] not in ("planned", "open"):
            raise PermissionError({"cooling": "仲喺冷靜期 — 確認咗先可以記錄買入",
                                   "cancelled": "呢個計劃已取消"}.get(t["status"], "已平倉 — 新交易請重新做 Pre-Trade Check"))
        if side == "SELL" and shares > (t["net_shares"] or 0) + 1e-9:
            raise ValueError(f"賣出股數 {shares:g} 多過持有 {t['net_shares'] or 0:g}")
        conn.execute("""INSERT INTO journal_fills (trade_id, side, filled_at, shares, price, commission, order_type, note, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (trade_id, side, filled_at, shares, price, _num(data.get("commission")) or 0.0,
                      data.get("order_type"), data.get("note"), _now_et().strftime("%Y-%m-%d %H:%M")))
        _recalc(conn, trade_id)
    return get_trade(trade_id)


def delete_fill(fill_id):
    with get_connection() as conn:
        f = conn.execute("SELECT trade_id FROM journal_fills WHERE id = ?", (fill_id,)).fetchone()
        if not f:
            raise LookupError("fill not found")
        conn.execute("DELETE FROM journal_fills WHERE id = ?", (fill_id,))
        _recalc(conn, f["trade_id"])
    return get_trade(f["trade_id"])


def save_review(trade_id, data):
    fp = _yn(data.get("followed_plan"))
    tags = [t for t in (data.get("error_tags") or []) if t in ERROR_TAGS]
    lesson = (data.get("lesson") or "").strip()
    errs = []
    if fp is None:
        errs.append("有冇跟足計劃？")
    if fp is False and not tags:
        errs.append("冇跟計劃 — 至少揀一個錯誤標籤")
    if "other" in tags and not (data.get("error_other") or "").strip():
        errs.append("錯誤標籤揀咗「其他」要寫明")
    if len(lesson) < 6:
        errs.append("教訓一句話必填")
    if errs:
        raise ValueError("；".join(errs))
    with get_connection() as conn:
        t = conn.execute("SELECT status FROM journal_trades WHERE id = ?", (trade_id,)).fetchone()
        if not t:
            raise LookupError("trade not found")
        if t["status"] not in ("closed", "reviewed"):
            raise PermissionError("平倉之後先做 Post-Trade Review")
        conn.execute("""UPDATE journal_trades SET followed_plan = ?, error_tags = ?, error_other = ?, review_notes = ?,
                        lesson = ?, reviewed_at = ?, status = 'reviewed', updated_at = ? WHERE id = ?""",
                     (1 if fp else 0, json.dumps(tags), data.get("error_other"), data.get("review_notes"), lesson,
                      _now_et().strftime("%Y-%m-%d %H:%M"), _now_et().strftime("%Y-%m-%d %H:%M"), trade_id))
    return get_trade(trade_id)


def cancel_trade(trade_id, reason=None):
    with get_connection() as conn:
        t = conn.execute("SELECT status FROM journal_trades WHERE id = ?", (trade_id,)).fetchone()
        if not t:
            raise LookupError("trade not found")
        if t["status"] not in ("cooling", "planned"):
            raise PermissionError("只可以取消未成交嘅計劃")
        conn.execute("UPDATE journal_trades SET status = 'cancelled', notes = COALESCE(notes || char(10), '') || ?, "
                     "updated_at = ? WHERE id = ?",
                     (f"Cancelled: {reason or '入唔到貨'}", _now_et().strftime("%Y-%m-%d %H:%M"), trade_id))
    return get_trade(trade_id)


def delete_trade(trade_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM journal_fills WHERE trade_id = ?", (trade_id,))
        n = conn.execute("DELETE FROM journal_trades WHERE id = ?", (trade_id,)).rowcount
    return n > 0


def get_trade(trade_id):
    with get_connection() as conn:
        t = conn.execute("SELECT * FROM journal_trades WHERE id = ?", (trade_id,)).fetchone()
        if not t:
            return None
        d = _row(t)
        d["fills"] = [dict(f) for f in conn.execute(
            "SELECT * FROM journal_fills WHERE trade_id = ? ORDER BY filled_at, id", (trade_id,))]
    return d


def list_trades(status=None):
    with get_connection() as conn:
        q = "SELECT * FROM journal_trades"
        args = ()
        if status:
            q += " WHERE status = ?"
            args = (status,)
        rows = [_row(r) for r in conn.execute(q + " ORDER BY COALESCE(opened_at, created_at) DESC, id DESC", args)]
        fills = {}
        for f in conn.execute("SELECT * FROM journal_fills ORDER BY filled_at, id"):
            fills.setdefault(f["trade_id"], []).append(dict(f))
    for r in rows:
        r["fills"] = fills.get(r["id"], [])
    return rows


def stats():
    s = get_settings()
    return compute_stats(list_trades(), s.get("fx_hkd"))


def meta():
    return {"config": {k: v for k, v in CONFIG.items() if k != "VAGUE_PHRASES"},
            "setup_types": SETUP_TYPES, "support_sources": SUPPORT_SOURCES, "stab_signs": STAB_SIGNS,
            "error_tags": ERROR_TAGS, "order_types": ORDER_TYPES, "event_types": EVENT_TYPES,
            "settings": get_settings(), "today_et": _now_et().strftime("%Y-%m-%d"),
            "now_et": _now_et().strftime("%Y-%m-%d %H:%M")}
