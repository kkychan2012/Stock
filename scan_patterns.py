"""
All 5 pattern scanners. Reads from stocks_daily; writes to pattern_scan_results.
"""

from datetime import datetime
from db_setup import get_connection

# ── Tuneable config ────────────────────────────────────────────────────────────
CUP_LOOKBACK        = 60     # trading days for cup window
CUP_LEFT_RIM_WIN    = 10     # days at start/end of cup to find rims
CUP_MIN_DEPTH       = 0.10   # 10% minimum cup depth
CUP_RIM_TOLERANCE   = 0.03   # right rim ≤ 3% from left rim
CUP_HANDLE_MIN      = 5      # minimum handle days
CUP_HANDLE_MAX      = 10     # maximum handle days
CUP_HANDLE_DROP_MIN = 0.03   # handle pulls back at least 3%
CUP_HANDLE_DROP_MAX = 0.08   # handle pulls back at most 8%
CUP_VOL_MULT        = 1.5    # breakout volume ≥ 1.5× Vol_MA10

MA200_VOL_MULT      = 1.2    # MA200 breakout volume ≥ 1.2× Vol_MA10
VOL_SURGE_MULT      = 1.5    # volume surge ≥ 1.5× Vol_MA10
PULLBACK_PCT        = 0.05   # within 5% above Low_30D
# ──────────────────────────────────────────────────────────────────────────────


def _load_prices_up_to(scan_date: str) -> dict:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT ticker, date, close, volume,
                      ma10, ma30, ma50, ma200,
                      high_30d, low_30d, vol_ma10, pct_change, direction
               FROM stocks_daily
               WHERE date <= ? AND close IS NOT NULL
               ORDER BY ticker, date""",
            (scan_date,)
        ).fetchall()
    data: dict = {}
    for r in rows:
        data.setdefault(r["ticker"], []).append(dict(r))
    return data


def _ef(row: dict) -> dict:
    """Extract standard display fields from a price row."""
    return {
        "signal_date": row.get("date"),
        "close":       row.get("close"),
        "ma10":        row.get("ma10"),
        "ma30":        row.get("ma30"),
        "ma50":        row.get("ma50"),
        "ma200":       row.get("ma200"),
        "volume":      row.get("volume"),
        "vol_ma10":    row.get("vol_ma10"),
        "high_30d":    row.get("high_30d"),
        "low_30d":     row.get("low_30d"),
        "pct_change":  row.get("pct_change"),
    }


# ── Pattern 1: Cup & Handle ────────────────────────────────────────────────────
def _cup_handle(ticker: str, rows: list):
    total = CUP_LOOKBACK + CUP_HANDLE_MAX + 1
    if len(rows) < total:
        return None
    w      = rows[-total:]
    cup    = w[:CUP_LOOKBACK]
    handle = w[CUP_LOOKBACK:-1]
    brk    = w[-1]

    closes = [r["close"] for r in cup if r["close"] is not None]
    if len(closes) < CUP_LOOKBACK:
        return None

    left_rim  = max(closes[:CUP_LEFT_RIM_WIN])
    mid       = closes[CUP_LEFT_RIM_WIN:-CUP_LEFT_RIM_WIN]
    cup_bot   = min(mid) if mid else min(closes)
    depth     = (left_rim - cup_bot) / left_rim
    if depth < CUP_MIN_DEPTH:
        return None

    right_rim = max(closes[-CUP_LEFT_RIM_WIN:])
    if abs(right_rim - left_rim) / left_rim > CUP_RIM_TOLERANCE:
        return None

    h_closes = [r["close"] for r in handle if r["close"] is not None]
    if len(h_closes) < CUP_HANDLE_MIN:
        return None
    h_drop = (right_rim - min(h_closes)) / right_rim
    if not (CUP_HANDLE_DROP_MIN <= h_drop <= CUP_HANDLE_DROP_MAX):
        return None

    rim_high = max(left_rim, right_rim)
    if brk["close"] <= rim_high:
        return None
    if brk.get("vol_ma10") and brk.get("volume"):
        if brk["volume"] < CUP_VOL_MULT * brk["vol_ma10"]:
            return None

    return {
        "ticker": ticker, "pattern_name": "Cup & Handle",
        "signal_detail": (
            f"Rim ${rim_high:.2f}, depth {depth*100:.1f}%, "
            f"handle {h_drop*100:.1f}% pullback"
        ),
        **_ef(brk),
    }


# ── Pattern 2: Golden Cross ────────────────────────────────────────────────────
def _golden_cross(ticker: str, rows: list):
    if len(rows) < 2:
        return None
    p, l = rows[-2], rows[-1]
    details = []
    if all(x.get(k) is not None for x in (p, l) for k in ("ma10", "ma30")):
        if p["ma10"] <= p["ma30"] and l["ma10"] > l["ma30"]:
            details.append("MA10 crossed above MA30")
    if all(x.get(k) is not None for x in (p, l) for k in ("ma50", "ma200")):
        if p["ma50"] <= p["ma200"] and l["ma50"] > l["ma200"]:
            details.append("MA50 crossed above MA200")
    if not details:
        return None
    return {
        "ticker": ticker, "pattern_name": "Golden Cross",
        "signal_detail": "; ".join(details),
        **_ef(l),
    }


# ── Pattern 3: MA200 Breakout ──────────────────────────────────────────────────
def _ma200_breakout(ticker: str, rows: list):
    if len(rows) < 2:
        return None
    p, l = rows[-2], rows[-1]
    if any(x.get(k) is None for x in (p, l) for k in ("close", "ma200")):
        return None
    if not (p["close"] <= p["ma200"] and l["close"] > l["ma200"]):
        return None
    if l.get("vol_ma10") and l.get("volume"):
        if l["volume"] < MA200_VOL_MULT * l["vol_ma10"]:
            return None
    if l.get("ma50") is not None and l.get("ma200") is not None:
        if l["ma50"] <= l["ma200"]:
            return None
    return {
        "ticker": ticker, "pattern_name": "MA200 Breakout",
        "signal_detail": f"Close ${l['close']:.2f} crossed above MA200 ${l['ma200']:.2f}",
        **_ef(l),
    }


# ── Pattern 4: Volume Surge Breakout ──────────────────────────────────────────
def _volume_surge(ticker: str, rows: list):
    if len(rows) < 2:
        return None
    p, l = rows[-2], rows[-1]
    if l.get("close") is None or l.get("volume") is None:
        return None
    if l["close"] <= (p.get("close") or 0):
        return None
    if l.get("vol_ma10") and l["volume"] < VOL_SURGE_MULT * l["vol_ma10"]:
        return None
    if p.get("high_30d") and l["close"] <= p["high_30d"]:
        return None
    if (l.get("direction") or "").lower() != "up":
        return None
    ratio = l["volume"] / l["vol_ma10"] if l.get("vol_ma10") else 0
    prev_h = p.get("high_30d") or 0
    return {
        "ticker": ticker, "pattern_name": "Volume Surge",
        "signal_detail": f"Vol {ratio:.1f}× MA10, broke High30D ${prev_h:.2f}",
        **_ef(l),
    }


# ── Pattern 5: Pullback Bounce ─────────────────────────────────────────────────
def _pullback_bounce(ticker: str, rows: list):
    if len(rows) < 2:
        return None
    p, l = rows[-2], rows[-1]
    if l.get("close") is None or l.get("low_30d") is None:
        return None
    lo = l["low_30d"]
    if not (lo < l["close"] <= lo * (1 + PULLBACK_PCT)):
        return None
    if l.get("ma200") is None or l["close"] <= l["ma200"]:
        return None
    if p.get("close") is None or p["close"] >= l["close"]:
        return None
    if l.get("ma10") is None or l.get("ma30") is None or l["ma10"] <= l["ma30"]:
        return None
    pct = (l["close"] - lo) / lo * 100
    return {
        "ticker": ticker, "pattern_name": "Pullback Bounce",
        "signal_detail": f"Close ${l['close']:.2f}, {pct:.1f}% above Low30D ${lo:.2f}",
        **_ef(l),
    }


_SCANNERS = [_cup_handle, _golden_cross, _ma200_breakout, _volume_surge, _pullback_bounce]


def scan_date_range(from_date: str, to_date: str, progress_cb=None) -> list:
    """Scan all patterns for every trading day in [from_date, to_date].
    Each date's results are saved to the DB independently (same as single-date scan).
    """
    with get_connection() as conn:
        date_rows = conn.execute(
            "SELECT DISTINCT date FROM stocks_daily"
            " WHERE date >= ? AND date <= ? ORDER BY date",
            (from_date, to_date),
        ).fetchall()
    trading_dates = [r["date"] for r in date_rows]
    if not trading_dates:
        return []

    # Load full price history up to to_date once — avoids repeated DB queries
    all_prices_full = _load_prices_up_to(to_date)
    tickers         = list(all_prices_full.keys())
    total_work      = len(trading_dates) * len(tickers)
    done            = 0
    all_results     = []

    for scan_date in trading_dates:
        date_results = []
        for ticker in tickers:
            done += 1
            if progress_cb:
                progress_cb(done, total_work, ticker)
            rows_up_to = [r for r in all_prices_full[ticker] if r["date"] <= scan_date]
            if not rows_up_to:
                continue
            for fn in _SCANNERS:
                try:
                    r = fn(ticker, rows_up_to)
                    if r:
                        date_results.append({**r, "scan_date": scan_date})
                except Exception:
                    pass
        _save_results(scan_date, date_results)
        all_results.extend(date_results)

    return all_results


def scan_all_patterns(scan_date: str = None, progress_cb=None) -> list:
    if not scan_date:
        scan_date = datetime.now().strftime("%Y-%m-%d")
    all_prices = _load_prices_up_to(scan_date)
    total      = len(all_prices)
    results    = []
    for i, (ticker, rows) in enumerate(all_prices.items()):
        if progress_cb:
            progress_cb(i + 1, total, ticker)
        for fn in _SCANNERS:
            try:
                r = fn(ticker, rows)
                if r:
                    results.append({**r, "scan_date": scan_date})
            except Exception:
                pass
    _save_results(scan_date, results)
    return results


def _save_results(scan_date: str, results: list):
    with get_connection() as conn:
        conn.execute("DELETE FROM pattern_scan_results WHERE scan_date = ?", (scan_date,))
        for r in results:
            try:
                conn.execute("""
                    INSERT INTO pattern_scan_results
                        (scan_date, ticker, pattern_name, signal_detail,
                         signal_date, close, ma10, ma30, ma50, ma200,
                         volume, vol_ma10, high_30d, low_30d, pct_change)
                    VALUES
                        (:scan_date, :ticker, :pattern_name, :signal_detail,
                         :signal_date, :close, :ma10, :ma30, :ma50, :ma200,
                         :volume, :vol_ma10, :high_30d, :low_30d, :pct_change)
                """, r)
            except Exception:
                pass


def get_scan_results_range(from_date: str, to_date: str) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM pattern_scan_results
               WHERE scan_date >= ? AND scan_date <= ?
               ORDER BY scan_date DESC, pattern_name, ticker""",
            (from_date, to_date),
        ).fetchall()
    return [dict(r) for r in rows]


def get_scan_results(scan_date: str) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM pattern_scan_results
               WHERE scan_date = ?
               ORDER BY pattern_name, ticker""",
            (scan_date,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_available_scan_dates() -> list:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT scan_date, COUNT(*) AS count
               FROM pattern_scan_results
               GROUP BY scan_date
               ORDER BY scan_date DESC"""
        ).fetchall()
    return [dict(r) for r in rows]
