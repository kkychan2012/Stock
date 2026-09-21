"""
Synthetic-bar tests for surge_strategy.evaluate_position, the live-monitor schedule
(monitor_due) and refresh_live() on an injected price set (no network, scratch DB).

Run from the project root:  python scripts/test_surge_monitor.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import surge_strategy as ss

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    fails += 0 if cond else 1


def mk(closes, opens=None, start="2026-01-05"):
    d0 = datetime.strptime(start, "%Y-%m-%d")
    out = []
    for i, c in enumerate(closes):
        d = d0 + timedelta(days=i)
        o = opens[i] if opens and opens[i] is not None else c
        out.append({"date": d.strftime("%Y-%m-%d"), "open": o, "high": max(o, c), "low": min(o, c), "close": c})
    return out


BASE = [100.0] * 25                      # bars 0..24, MA21 = 100
BUY = "2026-01-29"                       # bar 24; evaluation starts at bar 25
END_FINAL = "2026-03-01"                 # a "today" after every bar -> all bars final


def ev(closes, opens=None, today=None, final=True, target=None, half=False):
    bars = mk(closes, opens)
    return ss.evaluate_position(bars, BUY, target, half, today or END_FINAL, final)


# 1. break then next open still below -> SELL, fill reference = that open
r = ev(BASE + [95.0, 96.0], opens=[None] * 25 + [None, 96.0])
check("close<MA21 then open<MA21 -> sell", r["alert_state"] == "sell" and r["sell_open"] == 96.0
      and r["break_date"] == "2026-01-30", r)

# 2. break, next open back above MA21 -> no sell, ok again
r = ev(BASE + [95.0, 101.0, 102.0], opens=[None] * 25 + [None, 101.0, 102.0])
check("break then open above MA21 -> no sell", r["alert_state"] == "ok", r)

# 3. latest FINAL close below MA21 -> pending (waiting for the next open)
r = ev(BASE + [99.0])
check("final close below MA21 (latest bar) -> pending warn with break date",
      r["alert_state"] == "below_ma21" and r["break_date"] == "2026-01-30", r)

# 4. today's forming bar trading below MA21 -> intraday warn, no break date
bars = mk(BASE + [100.0, 97.0])
r = ss.evaluate_position(bars, BUY, None, False, today=bars[-1]["date"], final_today=False)
check("intraday below MA21 -> warn, unconfirmed", r["alert_state"] == "below_ma21"
      and r["break_date"] is None and r["price_is_live"], r)

# 5. yesterday broke MA21 (final) + today's forming bar OPENED below it -> confirmed sell at the open
bars = mk(BASE + [95.0, 96.0], opens=[None] * 25 + [None, 96.0])
r = ss.evaluate_position(bars, BUY, None, False, today=bars[-1]["date"], final_today=False)
check("break yesterday + open below today (bar still forming) -> sell", r["alert_state"] == "sell", r)

# 6. catch-up: sell condition happened earlier, price has since recovered -> still reported
r = ev(BASE + [95.0, 96.0, 110.0, 112.0, 115.0], opens=[None] * 25 + [None, 96.0, None, None, None])
check("earlier confirmed sell still found after recovery (server was off)", r["alert_state"] == "sell", r)

# 7. buy day itself is excluded (break on the buy day doesn't count)
bars = mk(BASE[:24] + [95.0, 96.0, 100.0], opens=[None] * 25 + [96.0, None])   # bar 24 = buy day, breaks MA21
r = ss.evaluate_position(bars, BUY, None, False, END_FINAL, True)
check("break on the buy day itself is ignored", r["alert_state"] != "sell", r)

# 8. partial target: close >= target counts; intraday-only touch also flagged (today's date)
r = ev(BASE + [110.0, 116.0], target=115.0)
check("close >= +15% target -> target_hit_date", r["target_hit_date"] == "2026-01-31", r)
bars = mk(BASE + [100.0, 116.0])
r = ss.evaluate_position(bars, BUY, 115.0, False, today=bars[-1]["date"], final_today=False)
check("forming bar touching target -> flagged for today", r["target_hit_date"] == bars[-1]["date"], r)
r = ev(BASE + [110.0, 116.0], target=115.0, half=True)
check("no target flag once half is sold", r["target_hit_date"] is None, r)

# 9. not enough history / nothing after buy -> harmless
r = ss.evaluate_position(mk([100.0] * 5), "2026-01-05", 115.0, False, END_FINAL, True)
check("short history -> ok, no crash", r["alert_state"] == "ok", r)


# 10. schedule
def et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ss.NY)


ss.MONITOR.update(last_run_dt=None, post_close_done=None)
check("Fri 10:00 ET, never run -> due (interval)", ss.monitor_due(et(2026, 9, 18, 10, 0)) == "interval")
ss.MONITOR["last_run_dt"] = et(2026, 9, 18, 10, 0)
check("10:20 ET, ran 20 min ago -> not due", ss.monitor_due(et(2026, 9, 18, 10, 20)) is None)
check("10:31 ET, ran 31 min ago -> due", ss.monitor_due(et(2026, 9, 18, 10, 31)) == "interval")
check("09:20 ET pre-open -> not due", ss.monitor_due(et(2026, 9, 18, 9, 20)) is None)
check("16:25 ET -> post-close run", ss.monitor_due(et(2026, 9, 18, 16, 25)) == "post-close")
ss.MONITOR["post_close_done"] = "2026-09-18"
check("16:25 ET after post-close done -> not due", ss.monitor_due(et(2026, 9, 18, 16, 25)) is None)
check("Saturday -> never due", ss.monitor_due(et(2026, 9, 19, 11, 0)) is None)
nr = ss.next_run_estimate(et(2026, 9, 19, 11, 0))
check("Saturday next run = Monday 09:35 ET", nr is not None and nr.weekday() == 0 and (nr.hour, nr.minute) == (9, 35), nr)

# 11. refresh_live on an injected price set + scratch DB (no network)
import sqlite3
import db_setup

tmp = os.path.join(tempfile.gettempdir(), "surge_monitor_test.db")
for f in (tmp, tmp + "-wal", tmp + "-shm"):
    if os.path.exists(f):
        os.remove(f)
db_setup.DB_PATH = tmp
db_setup.setup_database()
c = sqlite3.connect(tmp)
c.execute("INSERT INTO surge_signals (ticker, surge_date, surge_close, status, drop_date, buy_level, window_days_left)"
          " VALUES ('ARM','2026-02-02',100,'armed','2026-02-03',100,4)")
c.execute("INSERT INTO surge_signals (ticker, surge_date, surge_close, status) VALUES ('POS','2026-01-20',100,'bought')")
c.execute("INSERT INTO surge_positions (signal_id, ticker, surge_date, surge_close, buy_date, buy_price, partial_target, status)"
          " VALUES (2,'POS','2026-01-20',100,?,100,115,'holding')", (BUY,))
c.commit()
c.close()

pos_bars = mk(BASE + [95.0, 96.0], opens=[None] * 25 + [None, 96.0])
# ARM: surge day 2026-02-02 (idx 28), red drop day 2026-02-03 (close 98 < open 99),
# then bars whose last High reaches the 100 limit
arm = mk([100.0] * 28 + [100.0, 98.0, 97.0, 99.0])
for b in arm:
    if b["date"] == "2026-02-03":
        b["open"], b["close"] = 99.0, 98.0
arm[-1]["high"] = 100.5
summary = ss.run_refresh("test", now=et(2026, 3, 1, 12, 0), bars_by_ticker={"POS": pos_bars, "ARM": arm})
c = sqlite3.connect(tmp)
c.row_factory = sqlite3.Row
p = c.execute("SELECT * FROM surge_positions").fetchone()
s = c.execute("SELECT * FROM surge_signals WHERE ticker='ARM'").fetchone()
check("refresh_live: position flagged SELL and stored", p["alert_state"] == "sell" and p["ma21_break_date"] == "2026-01-30"
      and p["last_price"] == 96.0 and p["alert_note"], dict(p))
check("refresh_live: armed signal triggered live", s["status"] == "triggered" and s["trigger_date"] == arm[-1]["date"], dict(s))
check("refresh_live: summary + MONITOR status recorded",
      summary["sell"] == ["POS"] and summary["triggered"] == ["ARM"] and ss.MONITOR["last_run"] is not None, summary)
c.close()

# 12. Drop Day: a "watching" signal is promoted to armed only once its red candle has CLOSED
db = sqlite3.connect(tmp)
db.execute("INSERT INTO surge_signals (ticker, surge_date, surge_close, status) VALUES ('WCH','2026-02-02',100,'watching')")
db.commit()
db.close()
wbars = mk([100.0] * 29 + [99.0], opens=[None] * 29 + [101.0])      # idx 28 = surge day (close 100); idx 29 = 2026-02-03 red candle
ss.refresh_live(now=et(2026, 2, 3, 12, 0), bars_by_ticker={"WCH": wbars})          # day still forming
db = sqlite3.connect(tmp)
db.row_factory = sqlite3.Row
w = db.execute("SELECT * FROM surge_signals WHERE ticker='WCH'").fetchone()
check("forming red candle does NOT promote to Drop Day", w["status"] == "watching", dict(w))
res = ss.refresh_live(now=et(2026, 2, 3, 17, 0), bars_by_ticker={"WCH": wbars})    # after the close
w = db.execute("SELECT * FROM surge_signals WHERE ticker='WCH'").fetchone()
check("closed red candle promotes watching -> armed", w["status"] == "armed" and w["drop_date"] == "2026-02-03"
      and w["window_days_left"] == 5 and w["buy_level"] == 100.0, dict(w))
check("summary lists the new drop day", res["drop_day"] == ["WCH"], res)
res = ss.refresh_live(now=et(2026, 2, 3, 17, 30), bars_by_ticker={"WCH": wbars})
check("a second pass does not repeat it", res["drop_day"] == [], res)
db.close()

# 13. Drop Day alerts: only fresh drop days (<= 1 trading day old), once per signal
import api_server
sigs = [{"id": 1, "status": "armed", "days_since_drop": 0, "drop_date": "2026-02-03", "buy_level": 100.0,
         "window_days_left": 5, "ticker": "WCH", "expires_in": None},
        {"id": 2, "status": "armed", "days_since_drop": 3, "drop_date": "2026-01-29", "buy_level": 50.0,
         "window_days_left": 2, "ticker": "OLD", "expires_in": None}]
al = api_server._surge_alerts(sigs, [])
check("drop-day alert only for the fresh one", [a["key"] for a in al] == ["sig1:drop"] and al[0]["level"] == "drop", al)

print(f"\n{'ALL PASSED' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
