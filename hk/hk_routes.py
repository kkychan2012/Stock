"""
Flask blueprint for the "HK Surge" dashboard tab — everything under /api/hk/...

Kept in its own file (registered by api_server.py with two lines) so the US code paths are
untouched. It reads/writes only hk/hk_stocks.db.

  Fetch (separate from the US Fetch button):
    POST /api/hk/fetch {mode: quick|full|shares, scan: true}   background job
    GET  /api/hk/fetch/status
  Surge Strategy board (same workflow as the US tab):
    GET  /api/hk/board            signals, positions, closed, alerts, monitor, account + sizing
    POST /api/hk/scan             scan stored HK data for new signals
    POST /api/hk/signals/<id>/bought | /dismiss
    POST /api/hk/positions/<id>/partial | /sell        DELETE /api/hk/positions/<id>
    GET  /api/hk/alerts
    GET  /api/hk/monitor/status   POST /api/hk/monitor/refresh
"""
import math
import os
import sqlite3
import sys
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import hk_fetch
import hk_surge as hs

bp = Blueprint("hk", __name__)

_scan_lock = threading.Lock()
_last_scan = {"at": None, "summary": None}

# ---------------------------------------------------------------------------
# Background fetch (kept apart from the US /api/fetch)
# ---------------------------------------------------------------------------
_fetch_lock = threading.Lock()
_fetch = {"running": False, "mode": None, "started": None, "finished": None, "ok": None, "error": None, "log": []}


def _emit(msg):
    line = f"{datetime.now(hs.HKT):%H:%M:%S} {msg}"
    _fetch["log"].append(line)
    del _fetch["log"][:-400]


def _run_fetch(mode, scan):
    conn = None
    try:
        conn = sqlite3.connect(hk_fetch.DB_PATH, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        hk_fetch.init_db(conn)
        hs.setup_db()
        if mode == "quick":
            tickers = hk_fetch.eligible_tickers(conn)
            _emit(f"Quick update: {len(tickers)} stocks pass the size/liquidity screen (last ~20 days)")
            ok, skipped, empty = hk_fetch.fetch_incremental(conn, tickers, emit=_emit)
            _emit(f"Done: {ok} updated, {skipped} skipped (no history), {len(empty)} returned nothing")
        elif mode == "full":
            _emit("Full refresh: downloading the HKEX securities list ...")
            secs = hk_fetch.scrape_hkex_equities()
            hk_fetch.write_ticker_file(hk_fetch.ALL_FILE, [(x["ticker"], x["name"]) for x in secs],
                                       "# Every HKEX-listed equity + REIT (HKD counters), from HKEX's List of Securities.")
            hk_fetch.save_security_list(conn, secs)
            tickers = hk_fetch.read_ticker_file(hk_fetch.ALL_FILE)
            _emit(f"{len(tickers)} listed stocks; downloading price history from {hk_fetch.DEFAULT_START} (about 5 min) ...")
            ok, empty = hk_fetch.fetch(conn, tickers, hk_fetch.DEFAULT_START, 60, emit=_emit)
            missing = [r[0] for r in conn.execute("SELECT ticker FROM hk_securities WHERE shares IS NULL")]
            if missing:
                _emit(f"Fetching share counts for {len(missing)} stocks that have none ...")
                hk_fetch.fetch_shares(conn, missing, emit=_emit)
            _emit(f"Done: {ok} stocks with prices, {len(empty)} without data")
        elif mode == "shares":
            tickers = [r[0] for r in conn.execute("SELECT ticker FROM hk_securities ORDER BY ticker")]
            _emit(f"Refreshing shares outstanding for {len(tickers)} stocks ...")
            ok = hk_fetch.fetch_shares(conn, tickers, emit=_emit)
            _emit(f"Done: {ok}/{len(tickers)} have a share count")
        else:
            raise ValueError(f"unknown mode {mode!r}")
        latest = conn.execute("SELECT MAX(date) FROM hk_daily").fetchone()[0]
        _emit(f"HK price data now runs through {latest}")
        if scan and mode != "shares":
            res = hs.scan_signals()
            if "error" in res:
                _emit(f"Scan skipped: {res['error']}")
            else:
                _last_scan["at"] = datetime.now(hs.HKT).strftime("%Y-%m-%d %H:%M:%S")
                _last_scan["summary"] = {"as_of": res["as_of"], "tickers_scanned": res["tickers_scanned"],
                                         "new": len(res["new"]), "changed": len(res["changed"])}
                _emit(f"Scan: {len(res['new'])} new signal(s), {len(res['changed'])} changed "
                      f"({res['filtered_out']} surges filtered out by market-cap/turnover)")
        _fetch["ok"] = True
    except Exception as exc:
        _fetch["ok"] = False
        _fetch["error"] = f"{type(exc).__name__}: {exc}"
        _emit(f"ERROR: {_fetch['error']}")
    finally:
        if conn:
            conn.close()
        _fetch["running"] = False
        _fetch["finished"] = datetime.now(hs.HKT).strftime("%Y-%m-%d %H:%M:%S")
        _fetch_lock.release()


@bp.route("/api/hk/fetch", methods=["POST"])
def hk_fetch_start():
    b = request.get_json(silent=True) or {}
    mode = b.get("mode", "quick")
    if mode not in ("quick", "full", "shares"):
        return jsonify({"error": "mode must be quick, full or shares"}), 400
    if not _fetch_lock.acquire(blocking=False):
        return jsonify({"error": "A HK fetch is already running"}), 409
    _fetch.update(running=True, mode=mode, started=datetime.now(hs.HKT).strftime("%Y-%m-%d %H:%M:%S"),
                  finished=None, ok=None, error=None, log=[])
    threading.Thread(target=_run_fetch, args=(mode, bool(b.get("scan", True))), daemon=True, name="hk-fetch").start()
    return jsonify({"started": True, "mode": mode}), 202


@bp.route("/api/hk/fetch/status")
def hk_fetch_status():
    since = request.args.get("since", type=int, default=0)
    return jsonify({**{k: v for k, v in _fetch.items() if k != "log"}, "log": _fetch["log"][since:], "log_len": len(_fetch["log"])})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _num(val, name, required=True):
    if val in (None, ""):
        return (None, f"{name} is required") if required else (None, None)
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None, f"{name} must be a number"
    if v <= 0:
        return None, f"{name} must be greater than 0"
    return v, None


def _date(val):
    if not val:
        return datetime.now(hs.HKT).strftime("%Y-%m-%d")
    s = str(val).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _trading_dates(conn, n=80):
    return sorted(r[0] for r in conn.execute("SELECT DISTINCT date FROM hk_daily ORDER BY date DESC LIMIT ?", (n,)))


def _latest_daily(conn, tickers):
    out = {}
    for t in tickers:
        rows = conn.execute("SELECT date, close FROM hk_daily WHERE ticker=? AND close IS NOT NULL "
                            "ORDER BY date DESC LIMIT 21", (t,)).fetchall()
        if rows:
            out[t] = {"date": rows[0]["date"], "close": rows[0]["close"],
                      "ma21": sum(r["close"] for r in rows) / 21 if len(rows) == 21 else None}
    return out


def _pos_cash_effects(p, c):
    """(gross outlay, cash received from partial sale, cash received from final sale) in HKD;
    only meaningful when the position has an `amount`."""
    amt, bp = p["amount"], p["buy_price"]
    if not amt or not bp:
        return None
    eff = amt / (1 + c)
    got_partial = eff * 0.5 * (p["partial_price"] / bp) * (1 - c) if p["partial_price"] else 0.0
    got_final = 0.0
    if p["status"] == "closed" and p["exit_price"]:
        got_final = eff * (0.5 if p["partial_price"] else 1.0) * (p["exit_price"] / bp) * (1 - c)
    return amt, got_partial, got_final


def _position_view(p, daily, c):
    d = dict(p)
    bp, last = d["buy_price"], daily.get(d["ticker"])
    use_live = (d.get("last_price") is not None and d.get("price_asof")
                and (not last or d["price_asof"] >= last["date"]))
    if use_live:
        price, price_date, ma21 = d["last_price"], d["price_asof"], d.get("last_ma21")
    else:
        price = last["close"] if last else None
        price_date = last["date"] if last else None
        ma21 = last["ma21"] if last else None
    d["last_close"], d["last_close_date"] = price, price_date
    d["price_is_live"] = bool(use_live and d.get("price_is_live"))
    d["ma21"] = round(ma21, 4) if ma21 is not None else None
    d["dist_ma21_pct"] = round((price - ma21) / ma21 * 100, 2) if price is not None and ma21 else None
    d["below_ma21"] = bool(price is not None and ma21 and price < ma21)
    closed = d["status"] == "closed"
    rest = d["exit_price"] if closed else price
    gross = None
    if bp and rest:
        gross = (0.5 * (d["partial_price"] / bp - 1) + 0.5 * (rest / bp - 1)) if d["partial_price"] else (rest / bp - 1)
    d["pnl_pct"] = round(gross * 100, 2) if gross is not None else None
    d["pnl_final"] = closed
    net = None
    if gross is not None:
        if d.get("amount"):
            eff = d["amount"] / (1 + c)
            proceeds = eff * ((0.5 * d["partial_price"] / bp + 0.5 * rest / bp) if d["partial_price"] else rest / bp) * (1 - c)
            d["pnl_hkd"] = round(proceeds - d["amount"], 2)
            net = d["pnl_hkd"] / d["amount"]
        else:
            net = gross - 2 * c
    d["pnl_net_pct"] = round(net * 100, 2) if net is not None else None
    return d


def _positions_view(conn):
    c = hs.CONFIG["COST_PER_SIDE_PCT"] / 100
    rows = conn.execute("SELECT * FROM hk_surge_positions ORDER BY (status='closed'), buy_date DESC, id DESC").fetchall()
    daily = _latest_daily(conn, {r["ticker"] for r in rows})
    return [_position_view(r, daily, c) for r in rows]


def _attach_signal_price(s, last):
    """Current price for a signal: the live monitor's price when it is at least as recent as the stored
    daily close, otherwise the stored close. Adds distance from the buy level / surge close in %."""
    live = (s.get("last_price") is not None and s.get("price_asof")
            and (not last or s["price_asof"] >= last["date"]))
    price = s["last_price"] if live else (last["close"] if last else None)
    s["current_price"] = price
    s["price_date"] = s["price_asof"] if live else (last["date"] if last else None)
    s["price_is_live"] = bool(live and s.get("price_is_live"))
    lvl, sc = s.get("buy_level"), s.get("surge_close")
    s["vs_level_pct"] = round((price / lvl - 1) * 100, 2) if price and lvl else None
    s["vs_surge_pct"] = round((price / sc - 1) * 100, 2) if price and sc else None


def _signals_with_age(conn):
    cfg = hs.CONFIG
    tdates = _trading_dates(conn)
    signals = [dict(r) for r in conn.execute(
        "SELECT * FROM hk_surge_signals WHERE status IN ('watching','armed','triggered') ORDER BY surge_date DESC, ticker")]
    lots = {r["ticker"]: r["board_lot"] for r in conn.execute("SELECT ticker, board_lot FROM hk_securities")}
    for s in signals:
        s["board_lot"] = lots.get(s["ticker"])
        s["days_since_surge"] = sum(1 for d in tdates if d > s["surge_date"])
        s["days_since_drop"] = sum(1 for d in tdates if d > s["drop_date"]) if s["drop_date"] else None
        if s["trigger_date"]:
            since = sum(1 for d in tdates if d > s["trigger_date"])
            s["days_since_trigger"] = since
            stale = cfg["STALE_AFTER_TRIGGER_DAYS"]
            s["expires_in"] = (stale - since) if stale is not None else None
    daily = _latest_daily(conn, {s["ticker"] for s in signals})
    for s in signals:
        _attach_signal_price(s, daily.get(s["ticker"]))
    return signals


def _account(positions):
    """Account state under the sizing rule: SLOTS slots of BASE_STAKE, profit spread evenly over the free slots,
    one position never above MAX_POS_PCT of equity. Only positions with an HKD `amount` are counted."""
    cfg, c = hs.CONFIG, hs.CONFIG["COST_PER_SIDE_PCT"] / 100
    slots, base = cfg["SLOTS"], cfg["BASE_STAKE"]
    start = slots * base
    cash, cost_basis, realized, tracked, untracked, open_n = start, 0.0, 0.0, 0, 0, 0
    for p in positions:
        is_open = p["status"] != "closed"
        open_n += 1 if is_open else 0
        fx = _pos_cash_effects(p, c)
        if fx is None:
            untracked += 1 if is_open else 0
            continue
        tracked += 1
        amt, got_partial, got_final = fx
        cash += -amt + got_partial + got_final
        if is_open:
            cost_basis += amt * (0.5 if p["status"] == "partial_sold" else 1.0)
        else:
            realized += got_partial + got_final - amt
    free = max(0, slots - open_n)
    equity = cash + cost_basis
    stake = 0.0
    if free > 0:
        stake = base + max(0.0, cash - base * free) / free
        stake = min(stake, cfg["MAX_POS_PCT"] * equity)
        stake = min(stake, cash)
        if stake < 1:
            stake = 0.0
    return {"slots": slots, "base_stake": base, "start_capital": start, "cash": round(cash, 2),
            "open_positions": open_n, "free_slots": free, "equity_at_cost": round(equity, 2),
            "realized_pl": round(realized, 2), "tracked_positions": tracked, "untracked_open": untracked,
            "next_stake": round(stake, 2), "max_pos_pct": cfg["MAX_POS_PCT"], "cost_per_side_pct": cfg["COST_PER_SIDE_PCT"]}


def _suggest(signal, stake):
    """Amount / shares / board lots to buy for a signal at its limit price."""
    price, lot = signal.get("buy_level"), signal.get("board_lot") or 0
    if not price or stake <= 0:
        return {"amount": 0, "shares": 0, "lots": 0, "note": "no free slot" if stake <= 0 else None}
    c = hs.CONFIG["COST_PER_SIDE_PCT"] / 100
    raw = stake / (1 + c) / price
    if lot:
        lots = math.floor(raw / lot)
        shares = lots * lot
    else:
        lots, shares = None, math.floor(raw)
    note = None
    if lot and lots == 0:
        note = f"one board lot ({lot:,} sh) costs about HK${lot * price:,.0f} - more than the HK${stake:,.0f} stake"
    return {"amount": round(shares * price * (1 + c), 2), "shares": shares, "lots": lots, "lot_size": lot,
            "lot_value": round(lot * price, 2) if lot else None, "note": note}


def _alerts(signals, positions):
    out = []
    for p in positions:
        if p["status"] == "closed":
            continue
        if p["alert_state"] == "sell":
            out.append({"key": f"hkpos{p['id']}:sell:{p['ma21_break_date']}", "level": "sell", "ticker": p["ticker"],
                        "message": p["alert_note"] or "MA21 cut-loss confirmed - sell"})
        elif p["alert_state"] == "below_ma21":
            out.append({"key": f"hkpos{p['id']}:warn:{p['ma21_break_date'] or p['price_asof']}", "level": "warn",
                        "ticker": p["ticker"], "message": p["alert_note"] or "below MA21"})
        if p["status"] == "holding" and p.get("target_hit_date"):
            out.append({"key": f"hkpos{p['id']}:target", "level": "target", "ticker": p["ticker"],
                        "message": f"+15% target HK${p['partial_target']:.3f} reached ({p['target_hit_date']}) - sell half"})
    for s in signals:
        if s["status"] == "armed" and s.get("days_since_drop") is not None and s["days_since_drop"] <= 1:
            out.append({"key": f"hksig{s['id']}:drop", "level": "drop", "ticker": s["ticker"],
                        "message": f"Drop Day {s['drop_date']} (red candle) - buy level HK${s['buy_level']:.3f} "
                                   f"armed for {s['window_days_left']} more trading day(s)"})
        if s["status"] == "triggered" and (s.get("expires_in") is None or s["expires_in"] >= 0):
            out.append({"key": f"hksig{s['id']}:buy", "level": "buy", "ticker": s["ticker"],
                        "message": f"Buy Signal - limit HK${s['buy_level']:.3f} reached ({s['trigger_date']})"})
    return out


def _monitor_status():
    clock = hs.hk_clock()
    nxt = hs.next_run_estimate()
    m = hs.MONITOR
    return {"enabled": m["enabled"], "running": m["running"], "last_run": m["last_run"], "last_reason": m["last_reason"],
            "last_error": m["last_error"], "last_summary": m["last_summary"], "market_open": clock["is_open"],
            "now_hkt": clock["now"].strftime("%Y-%m-%d %H:%M"),
            "next_run": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None}


# ---------------------------------------------------------------------------
# Board / scan / alerts
# ---------------------------------------------------------------------------

@bp.route("/api/hk/board")
def hk_board():
    conn = hs.connect()
    try:
        conn.executescript(hs.SCHEMA)
        signals = _signals_with_age(conn)
        positions = _positions_view(conn)
        account = _account(positions)
        for s in signals:
            if s["status"] in ("triggered", "armed"):
                s["suggest"] = _suggest(s, account["next_stake"])
        counts = {st: 0 for st in ("watching", "armed", "triggered")}
        for s in signals:
            counts[s["status"]] += 1
        latest = conn.execute("SELECT MAX(date) FROM hk_daily").fetchone()[0]
        n_tickers = conn.execute("SELECT COUNT(DISTINCT ticker) FROM hk_daily").fetchone()[0]
    finally:
        conn.close()
    return jsonify({
        "signals": signals,
        "positions": [p for p in positions if p["status"] != "closed"],
        "closed": [p for p in positions if p["status"] == "closed"],
        "counts": counts, "config": hs.CONFIG, "account": account,
        "data_through": latest, "tickers_in_db": n_tickers,
        "last_scan": _last_scan, "alerts": _alerts(signals, positions),
        "monitor": _monitor_status(),
        "fetch": {k: v for k, v in _fetch.items() if k != "log"},
    })


@bp.route("/api/hk/alerts")
def hk_alerts():
    conn = hs.connect()
    try:
        conn.executescript(hs.SCHEMA)
        signals = _signals_with_age(conn)
        positions = _positions_view(conn)
    finally:
        conn.close()
    return jsonify({"alerts": _alerts(signals, positions), "monitor": _monitor_status()})


@bp.route("/api/hk/scan", methods=["POST"])
def hk_scan():
    if not _scan_lock.acquire(blocking=False):
        return jsonify({"error": "A HK scan is already running"}), 409
    try:
        summary = hs.scan_signals()
    finally:
        _scan_lock.release()
    if "error" in summary:
        return jsonify(summary), 400
    _last_scan["at"] = datetime.now(hs.HKT).strftime("%Y-%m-%d %H:%M:%S")
    _last_scan["summary"] = {"as_of": summary["as_of"], "tickers_scanned": summary["tickers_scanned"],
                             "new": len(summary["new"]), "changed": len(summary["changed"])}
    return jsonify({**summary, "at": _last_scan["at"]})


@bp.route("/api/hk/monitor/status")
def hk_monitor_status():
    return jsonify(_monitor_status())


@bp.route("/api/hk/monitor/refresh", methods=["POST"])
def hk_monitor_refresh():
    try:
        summary = hs.run_refresh("manual")
    except Exception as exc:
        return jsonify({"error": f"live refresh failed: {exc}"}), 502
    if summary is None:
        return jsonify({"error": "A live refresh is already running"}), 409
    return jsonify(summary)


# ---------------------------------------------------------------------------
# Signals / positions
# ---------------------------------------------------------------------------

@bp.route("/api/hk/signals/<int:sid>/bought", methods=["POST"])
def hk_mark_bought(sid):
    b = request.get_json(silent=True) or {}
    price, err = _num(b.get("buy_price"), "buy_price")
    if err:
        return jsonify({"error": err}), 400
    shares, err = _num(b.get("shares"), "shares", required=False)
    if err:
        return jsonify({"error": err}), 400
    amount, err = _num(b.get("amount"), "amount", required=False)
    if err:
        return jsonify({"error": err}), 400
    if amount is None and shares is not None:
        amount = shares * price * (1 + hs.CONFIG["COST_PER_SIDE_PCT"] / 100)      # gross outlay incl. buy cost
    if shares is None and amount is not None:
        shares = amount / (1 + hs.CONFIG["COST_PER_SIDE_PCT"] / 100) / price
    buy_date = _date(b.get("buy_date"))
    if not buy_date:
        return jsonify({"error": "buy_date not recognised"}), 400
    conn = hs.connect()
    try:
        conn.executescript(hs.SCHEMA)
        sig = conn.execute("SELECT * FROM hk_surge_signals WHERE id=?", (sid,)).fetchone()
        if not sig:
            return jsonify({"error": "signal not found"}), 404
        if conn.execute("SELECT 1 FROM hk_surge_positions WHERE signal_id=?", (sid,)).fetchone():
            return jsonify({"error": "already marked as bought"}), 409
        target = round(sig["surge_close"] * (1 + hs.CONFIG["PARTIAL_TARGET_PCT"]), 4)
        cur = conn.execute(
            """INSERT INTO hk_surge_positions (signal_id, ticker, name, surge_date, surge_close, buy_date, buy_price,
                   shares, amount, partial_target, status, note) VALUES (?,?,?,?,?,?,?,?,?,?,'holding',?)""",
            (sid, sig["ticker"], sig["name"], sig["surge_date"], sig["surge_close"], buy_date, price, shares, amount,
             target, b.get("note") or None))
        conn.execute("UPDATE hk_surge_signals SET status='bought', updated_at=CURRENT_TIMESTAMP WHERE id=?", (sid,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "position_id": cur.lastrowid, "partial_target": target}), 201


@bp.route("/api/hk/signals/<int:sid>/dismiss", methods=["POST"])
def hk_dismiss(sid):
    conn = hs.connect()
    try:
        conn.executescript(hs.SCHEMA)
        cur = conn.execute("UPDATE hk_surge_signals SET status='dismissed', updated_at=CURRENT_TIMESTAMP "
                           "WHERE id=? AND status IN ('watching','armed','triggered','expired')", (sid,))
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        return jsonify({"error": "signal not found or already bought"}), 404
    return jsonify({"ok": True})


def _sale_body():
    b = request.get_json(silent=True) or {}
    price, err = _num(b.get("price"), "price")
    date = _date(b.get("date"))
    if not err and not date:
        err = "date not recognised"
    return b, price, date, err


@bp.route("/api/hk/positions/<int:pid>/partial", methods=["POST"])
def hk_partial(pid):
    b, price, date, err = _sale_body()
    if err:
        return jsonify({"error": err}), 400
    conn = hs.connect()
    try:
        p = conn.execute("SELECT * FROM hk_surge_positions WHERE id=?", (pid,)).fetchone()
        if not p:
            return jsonify({"error": "position not found"}), 404
        if p["status"] != "holding":
            return jsonify({"error": f"position is {p['status']}, expected holding"}), 409
        conn.execute("UPDATE hk_surge_positions SET status='partial_sold', partial_date=?, partial_price=?, "
                     "target_hit_date=NULL WHERE id=?", (date, price, pid))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@bp.route("/api/hk/positions/<int:pid>/sell", methods=["POST"])
def hk_sell(pid):
    b, price, date, err = _sale_body()
    if err:
        return jsonify({"error": err}), 400
    reason = (b.get("reason") or "manual").strip()[:40]
    conn = hs.connect()
    try:
        p = conn.execute("SELECT * FROM hk_surge_positions WHERE id=?", (pid,)).fetchone()
        if not p:
            return jsonify({"error": "position not found"}), 404
        if p["status"] == "closed":
            return jsonify({"error": "position already closed"}), 409
        conn.execute("UPDATE hk_surge_positions SET status='closed', exit_date=?, exit_price=?, exit_reason=? WHERE id=?",
                     (date, price, reason, pid))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@bp.route("/api/hk/positions/<int:pid>", methods=["DELETE"])
def hk_delete_position(pid):
    conn = hs.connect()
    try:
        p = conn.execute("SELECT signal_id FROM hk_surge_positions WHERE id=?", (pid,)).fetchone()
        if not p:
            return jsonify({"error": "position not found"}), 404
        conn.execute("DELETE FROM hk_surge_positions WHERE id=?", (pid,))
        if p["signal_id"]:
            conn.execute("UPDATE hk_surge_signals SET status='triggered', updated_at=CURRENT_TIMESTAMP "
                         "WHERE id=? AND status='bought'", (p["signal_id"],))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})
