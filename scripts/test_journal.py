"""
Tests for journal.py: support test / stabilization, chase detection, event window,
the Pre-Trade Check (errors + red warnings + risk_ack), FIFO P/L, stats, and the
DB workflow (cooldown, fills, review) on a throw-away database. No network.

Run from the project root:  python scripts/test_journal.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_setup

_tmp = tempfile.mkdtemp()
db_setup.DB_PATH = os.path.join(_tmp, "journal_test.db")
db_setup.setup_database()

import journal as J

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    fails += 0 if cond else 1


def bar(d, o, h, l, c):
    return {"date": d, "open": o, "high": h, "low": l, "close": c}


# --- support test / stabilization -------------------------------------------
falling = [bar(f"2026-10-{10 + i:02d}", 120 - i * 2, 121 - i * 2, 118 - i * 2, 119 - i * 2) for i in range(8)]
a = J.analyze_support(falling, 100)
check("support not tested while lows stay > 1.5% above", not a["tested"] and not a["stabilized"])

knife = falling + [bar("2026-10-20", 104, 104, 100.5, 101)]
a = J.analyze_support(knife, 100)
check("test detected (low within 1.5%)", a["tested"] and a["test_date"] == "2026-10-20")
check("plain red test bar = no stabilization sign", not a["stabilized"], a)

hammer = falling + [bar("2026-10-20", 103, 103.5, 99.8, 103.2)]
a = J.analyze_support(hammer, 100)
check("hammer at support = long lower shadow", a["signs"]["long_lower_shadow"] == "2026-10-20", a)

reclaim = knife + [bar("2026-10-21", 101, 105, 100.8, 104.6)]
a = J.analyze_support(reclaim, 100)
check("close above prior bar's high = reclaim", a["signs"]["reclaim_high"] == "2026-10-21", a)

hl = knife + [bar("2026-10-21", 101, 102, 100.9, 101.5), bar("2026-10-22", 101.5, 103, 101.2, 102.5)]
a = J.analyze_support(hl, 100)
check("two higher lows after the low = higher low", a["signs"]["higher_low"] == "2026-10-22", a)

broken = knife + [bar("2026-10-21", 100, 100.5, 97, 97.5)]
a = J.analyze_support(broken, 100)
check("close > 1% below support = broken, not stabilized", a["broken"] and not a["stabilized"])

# --- chase ------------------------------------------------------------------
c = J.detect_chase(178.36, 179.92, 170.24)
check("NVDA 11/9 entry flagged as chase (near high + bounce)", c["chasing"] and len(c["reasons"]) == 2, c)
c = J.detect_chase(171.0, 179.92, 170.24)
check("entry near the day's low is not a chase", not c["chasing"], c)

# --- events -----------------------------------------------------------------
check("trading days Mon->Tue = 1", J.trading_days_between("2026-11-09", "2026-11-10") == 1)
check("trading days Fri->Mon = 1", J.trading_days_between("2026-11-06", "2026-11-09") == 1)
flag, up = J.events_in_window("2026-11-09", "2026-11-17", [{"date": "2026-11-10", "type": "cpi", "label": "CPI"},
                                                         {"date": "2026-12-09", "type": "fomc", "label": "FOMC"}])
check("CPI tomorrow + earnings in 6 tdays flagged, FOMC in a month not",
      [e["type"] for e in flag] == ["cpi", "earnings"], flag)
check("past events ignored", not J.events_in_window("2026-11-11", None, [{"date": "2026-11-10", "type": "cpi", "label": "x"}])[0])

# --- evaluate ---------------------------------------------------------------
settings = {"capital": 50000, "default_commission": 1.0, "macro_events": []}
good = {"ticker": "abc", "thesis": "回踩 50 日線同前低重疊位，出現長下影線企穩", "setup_type": "pullback",
        "entry_price": 101, "shares": 50, "order_type": "Limit", "support_price": 99, "support_source": "ma",
        "support_tested": "yes", "stab_signs": ["long_lower_shadow"], "stop_price": 97, "target_price": 110,
        "events_checked": True, "news_driven": "no", "chasing": "no", "accept_no_fill": "yes",
        "day_high": 106, "day_low": 100.5}
r = J.evaluate(good, {}, settings, today="2026-11-02")
check("clean plan: no errors, no warnings", not r["errors"] and not r["warnings"], r)
check("dist / RR / max loss computed", r["computed"]["dist_support_pct"] == 1.98 and r["computed"]["rr"] == 2.25
      and r["computed"]["max_loss"] == 202.0, r["computed"])

r = J.evaluate({**good, "thesis": "博反彈"}, {}, settings, today="2026-11-02")
check("vague thesis refused", any(e["field"] == "thesis" for e in r["errors"]))
r = J.evaluate({**good, "stop_price": None}, {}, settings, today="2026-11-02")
check("missing stop refused", any(e["field"] == "stop_price" for e in r["errors"]))
r = J.evaluate({**good, "accept_no_fill": "no"}, {}, settings, today="2026-11-02")
check("not accepting a missed fill refused", any(e["field"] == "accept_no_fill" for e in r["errors"]))
r = J.evaluate(good, {}, {**settings, "capital": None}, today="2026-11-02")
check("no capital set refused", any(e["field"] == "capital" for e in r["errors"]))

r = J.evaluate({**good, "target_price": 105}, {}, settings, today="2026-11-02")
check("RR < 2 -> red warning + risk_ack required", any(w["code"] == "low_rr" for w in r["warnings"])
      and any(e["field"] == "risk_ack" for e in r["errors"]))
r = J.evaluate({**good, "target_price": 105, "risk_ack": True}, {}, settings, today="2026-11-02")
check("risk_ack clears the block, warning stays", not r["errors"] and r["warnings"])

r = J.evaluate({**good, "support_price": 90, "stop_price": 88, "target_price": 130}, {}, settings, today="2026-11-02")
check("entry > 5% above support -> red", any(w["code"] == "far_from_support" for w in r["warnings"]))
r = J.evaluate({**good, "stab_signs": []}, {}, settings, today="2026-11-02")
check("no stabilization sign -> knife warning", any(w["code"] == "knife" for w in r["warnings"]))
r = J.evaluate({**good, "earnings_date": "2026-11-05"}, {}, settings, today="2026-11-02")
check("earnings in 3 tdays -> red", any(w["code"] == "event_earnings" for w in r["warnings"]))
r = J.evaluate({**good, "news_driven": "yes", "event_developing": "yes", "event_stabilized": "no"}, {}, settings,
               today="2026-11-02")
check("news event still developing, not stable -> red", any(w["code"] == "news_unstable" for w in r["warnings"]))
r = J.evaluate({**good, "news_driven": "yes"}, {}, settings, today="2026-11-02")
check("news-driven needs the follow-up answers", {"event_developing", "event_stabilized"} <= {e["field"] for e in r["errors"]})
r = J.evaluate({**good, "entry_price": 105.5, "stop_price": 97, "target_price": 125}, {}, settings, today="2026-11-02")
check("auto-detected chase -> red even if user says no", any(w["code"] == "chasing" for w in r["warnings"]))
r = J.evaluate({**good, "shares": 400}, {}, settings, today="2026-11-02")
codes = {w["code"] for w in r["warnings"]}
check("oversized position + max loss > 2% -> red", {"oversized", "risk_too_big"} <= codes, codes)

# --- FIFO P/L ---------------------------------------------------------------
nv = [{"side": "BUY", "filled_at": "2026-11-09 10:05", "shares": 12, "price": 178.36, "commission": 1.00},
      {"side": "SELL", "filled_at": "2026-11-10 09:40", "shares": 12, "price": 166.89, "commission": 1.04}]
p = J.fifo_pnl(nv)
check("NVDA realized P/L = -139.68", p["realized_pl"] == -139.68, p)
check("NVDA return = -6.52%, held 1 day, flat", p["realized_pct"] == -6.52 and p["holding_days"] == 1
      and p["net_shares"] == 0, p)
p = J.fifo_pnl([{"side": "BUY", "filled_at": "2026-01-02", "shares": 10, "price": 10, "commission": 0},
                {"side": "BUY", "filled_at": "2026-01-03", "shares": 10, "price": 20, "commission": 0},
                {"side": "SELL", "filled_at": "2026-01-04", "shares": 15, "price": 30, "commission": 0}])
check("FIFO: 10@10 + 5@20 sold at 30 = +250, 5 left @20", p["realized_pl"] == 250 and p["net_shares"] == 5
      and p["avg_cost"] == 20, p)

# --- stats ------------------------------------------------------------------
tr = [{"status": "reviewed", "realized_pl": 300, "closed_at": "2026-10-05", "followed_plan": 1, "error_tags": []},
      {"status": "reviewed", "realized_pl": -100, "closed_at": "2026-10-20", "followed_plan": 0,
       "error_tags": ["chase", "knife"]},
      {"status": "closed", "realized_pl": -50, "closed_at": "2026-11-10", "followed_plan": None, "error_tags": []},
      {"status": "open", "realized_pl": None}]
s = J.compute_stats(tr, 7.8)
check("win rate 1/3", s["win_rate"] == 33.3 and s["count"] == 3, s)
check("payoff = 300 / 75 = 4", s["payoff_ratio"] == 4.0, s)
check("followed vs not", s["plan"]["followed"]["total_pl"] == 300 and s["plan"]["not_followed"]["total_pl"] == -100)
check("tag loss counted per tag", {t["tag"]: t["loss"] for t in s["tags"]} == {"chase": -100, "knife": -100})
check("monthly + cumulative", [(m["month"], m["pl"], m["cum"]) for m in s["monthly"]]
      == [("2026-10", 200, 200), ("2026-11", -50, 150)], s["monthly"])
check("one closed trade awaiting review", s["open_reviews"] == 1)

# --- DB workflow ------------------------------------------------------------
J.save_settings({"capital": 50000})
check("settings saved", J.get_settings()["capital"] == 50000)
bad, res = J.create_trade({**good, "thesis": ""}, ctx={})
check("server refuses an incomplete check", bad is None and res["errors"])
t0 = datetime(2026, 11, 2, 10, 0, tzinfo=J.NY)
t, _ = J.create_trade({**good, "news_driven": "yes", "event_developing": "no", "event_stabilized": "yes",
                       "planned_date": "2026-11-02"}, ctx={}, now=t0)
check("news-driven plan starts in cooling", t and t["status"] == "cooling", t and t["status"])
try:
    J.add_fill(t["id"], {"side": "BUY", "shares": 10, "price": 101})
    check("BUY refused during cooldown", False)
except PermissionError:
    check("BUY refused during cooldown", True)
try:
    J.confirm_trade(t["id"], now=t0 + timedelta(minutes=10))
    check("confirm refused before 30 min", False)
except PermissionError:
    check("confirm refused before 30 min", True)
t = J.confirm_trade(t["id"], now=t0 + timedelta(minutes=31))
check("confirm allowed after 30 min", t["status"] == "planned")
t = J.add_fill(t["id"], {"side": "BUY", "filled_at": "2026-11-02 10:40", "shares": 10, "price": 101, "commission": 1})
check("BUY -> open", t["status"] == "open" and t["net_shares"] == 10)
try:
    J.add_fill(t["id"], {"side": "SELL", "shares": 20, "price": 105})
    check("oversell refused", False)
except ValueError:
    check("oversell refused", True)
try:
    J.save_review(t["id"], {"followed_plan": "yes", "lesson": "patience pays"})
    check("review refused while open", False)
except PermissionError:
    check("review refused while open", True)
t = J.add_fill(t["id"], {"side": "SELL", "filled_at": "2026-11-05 15:00", "shares": 10, "price": 110, "commission": 1})
check("SELL all -> closed with P/L", t["status"] == "closed" and t["realized_pl"] == 88.0 and t["holding_days"] == 3, t)
try:
    J.save_review(t["id"], {"followed_plan": "no", "lesson": "x"})
    check("review needs tags when plan not followed + a lesson", False)
except ValueError:
    check("review needs tags when plan not followed + a lesson", True)
t = J.save_review(t["id"], {"followed_plan": "yes", "lesson": "等到支持位企穩先入，有耐性"})
check("review saved -> reviewed", t["status"] == "reviewed" and t["followed_plan"] == 1)
t = J.delete_fill(t["fills"][-1]["id"])
check("deleting the SELL reopens the position", t["status"] == "open" and t["realized_pl"] is None)
check("delete trade", J.delete_trade(t["id"]) and J.get_trade(t["id"]) is None)

print(f"\n{'ALL PASSED' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
