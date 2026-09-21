"""
Tests for hk_surge: Hong Kong market-hours schedule (incl. lunch break), the live refresh on
injected bars (scratch DB, no network), and the scan filters on a tiny synthetic market.

Run from the project root:  python hk/test_hk_surge.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import hk_surge as hs

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    fails += 0 if cond else 1


def hkt(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=hs.HKT)


# ---- schedule ----
c = hs.hk_clock(hkt(2026, 9, 21, 10, 0))
check("Mon 10:00 HKT market open", c["is_open"] and not c["is_final"])
check("Mon 12:30 HKT lunch break -> closed", not hs.hk_clock(hkt(2026, 9, 21, 12, 30))["is_open"])
check("Mon 13:30 HKT open again", hs.hk_clock(hkt(2026, 9, 21, 13, 30))["is_open"])
check("Mon 16:25 HKT bar final", hs.hk_clock(hkt(2026, 9, 21, 16, 25))["is_final"])
check("Sat never open", not hs.hk_clock(hkt(2026, 9, 19, 11, 0))["is_open"])

hs.MONITOR.update(last_run_dt=None, post_close_done=None)
check("10:00 never run -> interval", hs.monitor_due(hkt(2026, 9, 21, 10, 0)) == "interval")
check("09:30 before the 09:45 open check -> not due", hs.monitor_due(hkt(2026, 9, 21, 9, 30)) is None)
hs.MONITOR["last_run_dt"] = hkt(2026, 9, 21, 11, 30)
check("12:30 lunch -> not due even though 60 min passed", hs.monitor_due(hkt(2026, 9, 21, 12, 30)) is None)
check("13:00 after lunch, 90 min since last -> due", hs.monitor_due(hkt(2026, 9, 21, 13, 0)) == "interval")
hs.MONITOR["last_run_dt"] = hkt(2026, 9, 21, 15, 45)
check("16:25 -> post-close pass", hs.monitor_due(hkt(2026, 9, 21, 16, 25)) == "post-close")
hs.MONITOR["post_close_done"] = "2026-09-21"
check("16:25 after post-close done -> not due", hs.monitor_due(hkt(2026, 9, 21, 16, 25)) is None)
check("Sat not due", hs.monitor_due(hkt(2026, 9, 19, 11, 0)) is None)
nr = hs.next_run_estimate(hkt(2026, 9, 19, 11, 0))
check("Sat next run = Mon 09:45 HKT", nr is not None and nr.weekday() == 0 and (nr.hour, nr.minute) == (9, 45), nr)
hs.MONITOR["last_run_dt"] = hkt(2026, 9, 21, 11, 45)
nr = hs.next_run_estimate(hkt(2026, 9, 21, 11, 50))
check("next run skips the lunch break (-> 12:55)", nr is not None and (nr.hour, nr.minute) == (12, 55), nr)

# ---- scratch DB ----
tmp = os.path.join(tempfile.gettempdir(), "hk_surge_test.db")
for f in (tmp, tmp + "-wal", tmp + "-shm"):
    if os.path.exists(f):
        os.remove(f)
hs.DB_PATH = tmp
hs.setup_db()


def mk(closes, opens=None, start="2026-01-05"):
    d0 = datetime.strptime(start, "%Y-%m-%d")
    out = []
    for i, cl in enumerate(closes):
        o = opens[i] if opens and opens[i] is not None else cl
        out.append({"date": (d0 + timedelta(days=i)).strftime("%Y-%m-%d"), "open": o,
                    "high": max(o, cl), "low": min(o, cl), "close": cl})
    return out


BASE = [100.0] * 25
db = sqlite3.connect(tmp)
db.execute("INSERT INTO hk_surge_signals (ticker, surge_date, surge_close, status, drop_date, buy_level, window_days_left)"
           " VALUES ('0001.HK','2026-02-02',100,'armed','2026-02-03',100,4)")
db.execute("INSERT INTO hk_surge_signals (ticker, surge_date, surge_close, status) VALUES ('0002.HK','2026-01-20',100,'bought')")
db.execute("INSERT INTO hk_surge_positions (signal_id, ticker, surge_date, surge_close, buy_date, buy_price, partial_target, status)"
           " VALUES (2,'0002.HK','2026-01-20',100,'2026-01-29',100,115,'holding')")
db.commit()
db.close()

pos_bars = mk(BASE + [95.0, 96.0], opens=[None] * 25 + [None, 96.0])
arm = mk([100.0] * 28 + [100.0, 98.0, 97.0, 99.0])
for b in arm:
    if b["date"] == "2026-02-03":
        b["open"], b["close"] = 99.0, 98.0
arm[-1]["high"] = 100.5
summary = hs.run_refresh("test", now=hkt(2026, 3, 1, 12, 0), bars_by_ticker={"0002.HK": pos_bars, "0001.HK": arm})
db = sqlite3.connect(tmp)
db.row_factory = sqlite3.Row
p = db.execute("SELECT * FROM hk_surge_positions").fetchone()
s = db.execute("SELECT * FROM hk_surge_signals WHERE ticker='0001.HK'").fetchone()
check("refresh: position SELL stored", p["alert_state"] == "sell" and p["ma21_break_date"] == "2026-01-30"
      and p["last_price"] == 96.0 and p["alert_note"], dict(p))
check("refresh: armed signal triggered live", s["status"] == "triggered" and s["trigger_date"] == arm[-1]["date"], dict(s))
check("refresh: summary + status recorded", summary["sell"] == ["0002.HK"] and summary["triggered"] == ["0001.HK"]
      and hs.MONITOR["last_run"] is not None, summary)
db.close()

# ---- scan filters on a synthetic market: one big+liquid surge, one small-cap surge, one illiquid surge ----
db = sqlite3.connect(tmp)
db.executescript("""
CREATE TABLE IF NOT EXISTS hk_daily (ticker TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
    volume INTEGER, vol_ma10 REAL, ma50 REAL, ma200 REAL, PRIMARY KEY (ticker, date));
CREATE TABLE IF NOT EXISTS hk_securities (ticker TEXT PRIMARY KEY, name TEXT, category TEXT, sub_category TEXT,
    board_lot INTEGER, shares REAL, shares_asof TEXT);
""")


def surge_stock(ticker, shares, base_vol):
    db.execute("INSERT OR REPLACE INTO hk_securities (ticker, name, shares) VALUES (?,?,?)", (ticker, ticker + " Ltd", shares))
    d0 = datetime.strptime("2026-05-01", "%Y-%m-%d")
    price = 10.0
    for i in range(60):
        d = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        vol = base_vol * (5 if i == 55 else 1)                 # day 55 = 5x volume surge, up day
        price = price + (1.0 if i == 55 else 0.0)
        vm = base_vol if i != 55 else (base_vol * 9 + vol) / 10
        db.execute("INSERT OR REPLACE INTO hk_daily VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (ticker, d, price, price, price, price, vol, vm, 9.0, 8.0))


surge_stock("1111.HK", 500_000_000, 1_000_000)     # price 10 x 500M = HK$5B cap, turnover 10M -> passes
surge_stock("2222.HK", 20_000_000, 1_000_000)      # HK$200M cap -> filtered out (too small)
surge_stock("3333.HK", 500_000_000, 100_000)       # turnover HK$1M -> filtered out (illiquid)
db.commit()
db.close()
res = hs.scan_signals(config={"MIN_TURNOVER": 5_000_000})
check("scan finds only the big + liquid surge", [n[0] for n in res["new"]] == ["1111.HK"] and res["filtered_out"] == 2, res)

# ---- Drop Day promotion (HK) ----
db = sqlite3.connect(tmp)
db.execute("INSERT INTO hk_surge_signals (ticker, surge_date, surge_close, status) VALUES ('0003.HK','2026-02-02',100,'watching')")
db.commit()
db.close()
wbars = mk([100.0] * 29 + [99.0], opens=[None] * 29 + [101.0])
hs.refresh_live(now=hkt(2026, 2, 3, 11, 0), bars_by_ticker={"0003.HK": wbars})        # HK day still forming
db = sqlite3.connect(tmp)
db.row_factory = sqlite3.Row
w = db.execute("SELECT * FROM hk_surge_signals WHERE ticker='0003.HK'").fetchone()
check("HK: forming red candle does not promote", w["status"] == "watching", dict(w))
res = hs.refresh_live(now=hkt(2026, 2, 3, 16, 30), bars_by_ticker={"0003.HK": wbars})  # after the HK close
w = db.execute("SELECT * FROM hk_surge_signals WHERE ticker='0003.HK'").fetchone()
check("HK: closed red candle promotes watching -> armed", w["status"] == "armed" and w["drop_date"] == "2026-02-03"
      and w["window_days_left"] == 5, dict(w))
check("HK: summary lists the drop day", res["drop_day"] == ["0003.HK"], res)
db.close()

import hk_routes
sigs = [{"id": 1, "status": "armed", "days_since_drop": 1, "drop_date": "2026-02-03", "buy_level": 3.045,
         "window_days_left": 4, "ticker": "0003.HK", "expires_in": None},
        {"id": 2, "status": "armed", "days_since_drop": 4, "drop_date": "2026-01-29", "buy_level": 5.0,
         "window_days_left": 1, "ticker": "0004.HK", "expires_in": None}]
al = hk_routes._alerts(sigs, [])
check("HK: drop-day alert only for the fresh one", [a["key"] for a in al] == ["hksig1:drop"] and "HK$3.045" in al[0]["message"], al)

# ---- current price on every active signal (HK) ----
db = sqlite3.connect(tmp)
db.execute("INSERT INTO hk_surge_signals (ticker, surge_date, surge_close, status, drop_date, buy_level, trigger_date) "
           "VALUES ('0005.HK','2026-02-02',100,'triggered','2026-02-03',100,'2026-02-04')")
db.commit()
db.close()
tbars = mk([100.0] * 28 + [100.0, 98.0, 103.0, 104.5])
hs.refresh_live(now=hkt(2026, 3, 1, 12, 0), bars_by_ticker={"0005.HK": tbars, "0003.HK": wbars})
db = sqlite3.connect(tmp)
db.row_factory = sqlite3.Row
t = db.execute("SELECT * FROM hk_surge_signals WHERE ticker='0005.HK'").fetchone()
check("HK: triggered signal gets its current price", t["last_price"] == 104.5 and t["price_asof"] == tbars[-1]["date"], dict(t))
w = db.execute("SELECT * FROM hk_surge_signals WHERE ticker='0003.HK'").fetchone()
check("HK: armed signal gets a price too", w["last_price"] == 99.0, dict(w))
hs.refresh_live(now=hkt(2026, 2, 5, 11, 0), bars_by_ticker={"0005.HK": tbars})
t = db.execute("SELECT * FROM hk_surge_signals WHERE ticker='0005.HK'").fetchone()
check("HK: today's forming bar is flagged live", t["price_is_live"] == 1, dict(t))
db.close()

last = {"date": "2026-02-05", "close": 104.5}
s1 = {"last_price": 106.0, "price_asof": "2026-02-06", "price_is_live": 1, "buy_level": 100.0, "surge_close": 100.0}
hk_routes._attach_signal_price(s1, last)
check("HK: live price newer than stored wins; +6% vs the level", s1["current_price"] == 106.0 and s1["vs_level_pct"] == 6.0, s1)
s2 = {"last_price": 90.0, "price_asof": "2026-02-01", "price_is_live": 0, "buy_level": 100.0, "surge_close": 100.0}
hk_routes._attach_signal_price(s2, last)
check("HK: stale live price loses to the newer stored close", s2["current_price"] == 104.5, s2)

# an existing table without the new columns is upgraded by connect()
old = os.path.join(tempfile.gettempdir(), "hk_old_schema.db")
for f in (old, old + "-wal", old + "-shm"):
    if os.path.exists(f):
        os.remove(f)
o = sqlite3.connect(old)
o.execute("CREATE TABLE hk_surge_signals (id INTEGER PRIMARY KEY, ticker TEXT, surge_date TEXT, surge_close REAL, status TEXT)")
o.commit()
o.close()
keep = hs.DB_PATH
hs.DB_PATH = old
cx = hs.connect()
cols = {r[1] for r in cx.execute("PRAGMA table_info(hk_surge_signals)")}
cx.close()
hs.DB_PATH = keep
check("HK: old signals table gets the price columns", {"last_price", "price_asof", "price_is_live", "last_checked"} <= cols, cols)
for f in (old, old + "-wal", old + "-shm"):
    if os.path.exists(f):
        os.remove(f)

# ---- scan ignores today's still-forming HK bar ----
import surge_strategy as _ss
data = {"0700.HK": [{"date": "2026-09-18", "open": 10, "close": 11}, {"date": "2026-09-21", "open": 11, "close": 10}]}
n = _ss.drop_forming_bars(data, hs.hk_clock(hkt(2026, 9, 21, 11, 0)))
check("HK: forming bar (11:00 HKT) is dropped", n == 1 and len(data["0700.HK"]) == 1)
data = {"0700.HK": [{"date": "2026-09-18", "open": 10, "close": 11}, {"date": "2026-09-21", "open": 11, "close": 10}]}
n = _ss.drop_forming_bars(data, hs.hk_clock(hkt(2026, 9, 21, 17, 0)))
check("HK: after 16:20 HKT the bar is kept", n == 0 and len(data["0700.HK"]) == 2)

print(f"\n{'ALL PASSED' if not fails else str(fails) + ' FAILED'}")
for f in (tmp, tmp + "-wal", tmp + "-shm"):
    if os.path.exists(f):
        os.remove(f)
sys.exit(1 if fails else 0)
