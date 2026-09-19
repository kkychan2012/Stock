"""
All 5 pattern scanners. Reads from stocks_daily; writes to pattern_scan_results.
"""

import json
from datetime import datetime
from db_setup import get_connection
from vcp_detector import detect_vcp

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
VOL_SURGE_MULT      = 2.0    # volume surge ≥ 2.0× Vol_MA10 (tuneable per-scan via the UI/API too)
PULLBACK_PCT        = 0.05   # within 5% above Low_30D

MOMENTUM_WINDOW     = 15     # rolling window in trading days
MOMENTUM_UP_DAYS    = 12     # minimum up-days in the window
MOMENTUM_VOL_EXP    = 1.25   # recent avg vol ≥ 1.25× prior window avg vol
MOMENTUM_PRICE_GAIN = 0.20   # close at end of window ≥ 20% above close at start

MOMENTUM_SHORT_WINDOW   = 10  # rolling window for 10/8 pattern
MOMENTUM_SHORT_UP_DAYS  = 8   # minimum up-days in the 10-day window

SQUEEZE_MA_PERIODS         = (9, 21, 50, 100)  # MAs whose convergence is tested
SQUEEZE_THRESHOLD          = 3.0  # max % spread between the tightest/widest MA
PRICE_THRESHOLD            = 2.0  # max % distance of close from the MA cluster midpoint
SQUEEZE_LOOKBACK_DAYS      = 60   # rolling window of spread_pct history used for days-in-squeeze / trend
MAX_SQUEEZE_AGE            = 10   # "fresh" cutoff: squeeze must have formed within this many trading days
SQUEEZE_TIGHTEN_RECENT_DAYS = 5   # "last N days" window in the tightening-trend check
SQUEEZE_TIGHTEN_PRIOR_DAYS  = 20  # "days 6-N back" window in the tightening-trend check
# ──────────────────────────────────────────────────────────────────────────────


def _load_prices_up_to(scan_date: str) -> dict:
    """Tracked tickers (stocks_daily) merged with the rest of the active S&P
    500 + Russell 1000 universe (universe_prices) — same "tracked wins on
    overlap" rule /api/data/summary uses, so the Pattern Scanner covers the
    full ~1,100+ ticker universe instead of only the tracked Ticker List."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT ticker, date, close, high, low, volume,
                      ma10, ma30, ma50, ma200,
                      high_30d, low_30d, vol_ma10, pct_change, direction,
                      trend_score
               FROM stocks_daily
               WHERE date <= ? AND close IS NOT NULL
               UNION ALL
               SELECT u.ticker, u.date, u.close, u.high, u.low, u.volume,
                      u.ma10, u.ma30, u.ma50, u.ma200,
                      u.high_30d, u.low_30d, u.vol_ma10, u.pct_change, u.direction,
                      u.trend_score
               FROM universe_prices u
               WHERE u.date <= ? AND u.close IS NOT NULL
                 AND u.ticker IN (SELECT ticker FROM ticker_universe WHERE is_active = 1)
                 AND u.ticker NOT IN (SELECT ticker FROM extraction_tickers)
               ORDER BY ticker, date""",
            (scan_date, scan_date)
        ).fetchall()
    data: dict = {}
    for r in rows:
        data.setdefault(r["ticker"], []).append(dict(r))
    return data


def _extra_mas(rows: list) -> dict:
    """MA9/MA21/MA100 for the latest row — not stored in stocks_daily, so computed
    here on close price history (same simple-MA approach as the MA Squeeze detector)."""
    closes = [r.get("close") for r in rows]
    out = {}
    for period, key in ((9, "ma9"), (21, "ma21"), (100, "ma100")):
        if len(closes) < period or any(c is None for c in closes[-period:]):
            out[key] = None
        else:
            out[key] = round(sum(closes[-period:]) / period, 4)
    return out


def _ef(rows: list) -> dict:
    """Extract standard display fields from a price history, for its latest row."""
    row = rows[-1]
    return {
        "signal_date": row.get("date"),
        "close":       row.get("close"),
        **_extra_mas(rows),
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
        **_ef(rows),
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
        **_ef(rows),
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
        **_ef(rows),
    }


# ── Pattern 4: Volume Surge Breakout ──────────────────────────────────────────
def _volume_surge(ticker: str, rows: list, vol_surge_mult: float = VOL_SURGE_MULT):
    if len(rows) < 2:
        return None
    p, l = rows[-2], rows[-1]
    if l.get("close") is None or l.get("volume") is None:
        return None
    if l["close"] <= (p.get("close") or 0):
        return None
    if l.get("vol_ma10") and l["volume"] < vol_surge_mult * l["vol_ma10"]:
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
        **_ef(rows),
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
        **_ef(rows),
    }


# ── Pattern 6: Momentum Expansion ─────────────────────────────────────────────
def _momentum_expansion(ticker: str, rows: list):
    needed = MOMENTUM_WINDOW * 2  # 30 rows: 15 recent + 15 prior
    if len(rows) < needed:
        return None

    recent_rows = rows[-MOMENTUM_WINDOW:]               # last 15 days
    prior_rows  = rows[-(MOMENTUM_WINDOW * 2):-MOMENTUM_WINDOW]  # days 16–30

    # Up-day count: need one extra row before the window for the first comparison
    if len(rows) < MOMENTUM_WINDOW + 1:
        return None
    window_for_updays = rows[-(MOMENTUM_WINDOW + 1):]  # 16 rows
    up_days = sum(
        1 for i in range(1, MOMENTUM_WINDOW + 1)
        if (window_for_updays[i].get("close") is not None
            and window_for_updays[i - 1].get("close") is not None
            and window_for_updays[i]["close"] > window_for_updays[i - 1]["close"])
    )
    if up_days < MOMENTUM_UP_DAYS:
        return None

    # Price gain: close at end of window ≥ 20% above close at start
    close_start = recent_rows[0].get("close")
    close_end   = recent_rows[-1].get("close")
    if close_start is None or close_end is None or close_start == 0:
        return None
    price_gain = (close_end - close_start) / close_start
    if price_gain < MOMENTUM_PRICE_GAIN:
        return None

    recent_vols = [r["volume"] for r in recent_rows if r.get("volume") is not None]
    prior_vols  = [r["volume"] for r in prior_rows  if r.get("volume") is not None]
    if not recent_vols or not prior_vols:
        return None

    avg_recent = sum(recent_vols) / len(recent_vols)
    avg_prior  = sum(prior_vols)  / len(prior_vols)
    if avg_prior == 0 or avg_recent < MOMENTUM_VOL_EXP * avg_prior:
        return None

    vol_ratio = avg_recent / avg_prior
    return {
        "ticker": ticker, "pattern_name": "Momentum Expansion",
        "signal_detail": (
            f"{up_days}/15 up-days, +{price_gain*100:.1f}% in 15D, "
            f"vol {vol_ratio:.2f}× prior avg"
        ),
        "mom_15d_start":    round(close_start, 4),
        "mom_15d_gain_pct": round(price_gain * 100, 2),
        **_ef(rows),
    }


# ── Pattern 7: Momentum 10/8 ──────────────────────────────────────────────────
def _momentum_10_8(ticker: str, rows: list):
    needed = MOMENTUM_SHORT_WINDOW * 2  # 20 rows: 10 recent + 10 prior
    if len(rows) < needed:
        return None

    recent_rows = rows[-MOMENTUM_SHORT_WINDOW:]
    prior_rows  = rows[-(MOMENTUM_SHORT_WINDOW * 2):-MOMENTUM_SHORT_WINDOW]

    # Up-day count: need one extra row before the window for the first comparison
    if len(rows) < MOMENTUM_SHORT_WINDOW + 1:
        return None
    window_for_updays = rows[-(MOMENTUM_SHORT_WINDOW + 1):]
    up_days = sum(
        1 for i in range(1, MOMENTUM_SHORT_WINDOW + 1)
        if (window_for_updays[i].get("close") is not None
            and window_for_updays[i - 1].get("close") is not None
            and window_for_updays[i]["close"] > window_for_updays[i - 1]["close"])
    )
    if up_days < MOMENTUM_SHORT_UP_DAYS:
        return None

    # Volume expansion: last 10D avg ≥ 1.25× prior 10D avg
    recent_vols = [r["volume"] for r in recent_rows if r.get("volume") is not None]
    prior_vols  = [r["volume"] for r in prior_rows  if r.get("volume") is not None]
    if not recent_vols or not prior_vols:
        return None

    avg_recent = sum(recent_vols) / len(recent_vols)
    avg_prior  = sum(prior_vols)  / len(prior_vols)
    if avg_prior == 0 or avg_recent < MOMENTUM_VOL_EXP * avg_prior:
        return None

    # Price gain over the 10-day window
    close_start = recent_rows[0].get("close")
    close_end   = recent_rows[-1].get("close")
    price_gain  = ((close_end - close_start) / close_start
                   if close_start and close_end and close_start != 0 else None)

    vol_ratio = avg_recent / avg_prior
    return {
        "ticker": ticker, "pattern_name": "Momentum 10/8",
        "signal_detail": (
            f"{up_days}/10 up-days"
            + (f", +{price_gain*100:.1f}% in 10D" if price_gain is not None else "")
            + f", vol {vol_ratio:.2f}× prior 10D avg"
        ),
        "mom_15d_start":    round(close_start, 4) if close_start else None,
        "mom_15d_gain_pct": round(price_gain * 100, 2) if price_gain is not None else None,
        **_ef(rows),
    }


# ── Pattern 8: MA Squeeze ──────────────────────────────────────────────────────
def _sma_series(closes: list, period: int) -> list:
    """Simple moving average aligned with `closes`; None where history is short."""
    out = []
    s = 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= period:
            s -= closes[i - period]
        out.append(s / period if i >= period - 1 else None)
    return out


def _ma_squeeze(ticker: str, rows: list,
                 squeeze_threshold: float = SQUEEZE_THRESHOLD,
                 price_threshold: float = PRICE_THRESHOLD,
                 max_squeeze_age: float = MAX_SQUEEZE_AGE):
    needed = max(SQUEEZE_MA_PERIODS) + SQUEEZE_LOOKBACK_DAYS
    if len(rows) < needed:
        return None
    closes = [r.get("close") for r in rows]
    if any(c is None for c in closes[-needed:]):
        return None

    mas = {p: _sma_series(closes, p) for p in SQUEEZE_MA_PERIODS}
    last_i = len(rows) - 1

    def spread_pct_at(i):
        vals = [mas[p][i] for p in SQUEEZE_MA_PERIODS]
        if any(v is None for v in vals):
            return None
        hi, lo = max(vals), min(vals)
        return (hi - lo) / lo * 100 if lo else None

    spread_today = spread_pct_at(last_i)
    if spread_today is None or spread_today > squeeze_threshold:
        return None

    vals_today  = [mas[p][last_i] for p in SQUEEZE_MA_PERIODS]
    cluster_mid = (max(vals_today) + min(vals_today)) / 2
    close_today = closes[last_i]
    if not cluster_mid:
        return None
    price_dist_pct = abs(close_today - cluster_mid) / cluster_mid * 100
    if price_dist_pct > price_threshold:
        return None

    # Rolling spread_pct history over the trailing lookback window, most-recent last.
    window_start   = max(0, last_i - SQUEEZE_LOOKBACK_DAYS + 1)
    spread_history = [spread_pct_at(i) for i in range(window_start, last_i + 1)]

    # days_in_squeeze: consecutive days counting back from today with spread_pct <= threshold
    days_in_squeeze = 0
    for s in reversed(spread_history):
        if s is not None and s <= squeeze_threshold:
            days_in_squeeze += 1
        else:
            break

    # Tightening trend: mean(last 5 days) < mean(days 6-20 back) — MAs still converging
    recent = [s for s in spread_history[-SQUEEZE_TIGHTEN_RECENT_DAYS:] if s is not None]
    prior  = [s for s in spread_history[-SQUEEZE_TIGHTEN_PRIOR_DAYS:-SQUEEZE_TIGHTEN_RECENT_DAYS]
              if s is not None]
    is_tightening = (bool(recent) and bool(prior)
                      and sum(recent) / len(recent) < sum(prior) / len(prior))

    is_fresh = days_in_squeeze <= max_squeeze_age and is_tightening

    detail = (
        f"Spread {spread_today:.1f}% · in squeeze {days_in_squeeze}d"
        f" · {'tightening' if is_tightening else 'expanding'}"
    )
    return {
        "ticker": ticker, "pattern_name": "MA Squeeze",
        "signal_detail": detail,
        "squeeze_spread_pct": round(spread_today, 2),
        "squeeze_days":       days_in_squeeze,
        "squeeze_fresh":      int(is_fresh),
        **_ef(rows),
    }


# ── Pattern 9: VCP (Volatility Contraction Pattern) ───────────────────────────
def _run_vcp(ticker: str, rows: list):
    """Runs the VCP detector once per ticker/date. Returns (vcp_signal_row, pattern_row).
    vcp_signal_row feeds vcp_signals (badge data for every pattern hit on this
    ticker/date); pattern_row is only set when a full VCP setup is detected, so
    it also shows up as its own "VCP" pattern row/sub-tab."""
    try:
        r = detect_vcp(ticker, rows)
    except Exception:
        return None, None
    if r.get("pivot_price") is None:
        return None, None

    last    = rows[-1]
    sig_row = {**r, "close": last.get("close")}

    pat_row = None
    if r["vcp_detected"]:
        depths = " → ".join(f"{d:.1f}%" for d in r["contraction_depths"])
        pat_row = {
            "ticker": ticker, "pattern_name": "VCP",
            "signal_detail": f"{r['num_contractions']} contractions ({depths}), pivot ${r['pivot_price']:.2f}",
            **_ef(rows),
        }
    return sig_row, pat_row


def _save_vcp_results(scan_date: str, results: list):
    with get_connection() as conn:
        conn.execute("DELETE FROM vcp_signals WHERE scan_date = ?", (scan_date,))
        for r in results:
            try:
                conn.execute("""
                    INSERT INTO vcp_signals
                        (scan_date, ticker, vcp_detected, num_contractions,
                         contraction_depths, volume_dryup, pivot_price,
                         suggested_stop, current_price_vs_pivot, tightness_pct, close)
                    VALUES
                        (:scan_date, :ticker, :vcp_detected, :num_contractions,
                         :contraction_depths, :volume_dryup, :pivot_price,
                         :suggested_stop, :current_price_vs_pivot, :tightness_pct, :close)
                """, {
                    "scan_date":              scan_date,
                    "ticker":                 r["ticker"],
                    "vcp_detected":           int(bool(r["vcp_detected"])),
                    "num_contractions":       r["num_contractions"],
                    "contraction_depths":     json.dumps(r["contraction_depths"]),
                    "volume_dryup":           int(bool(r["volume_dryup"])),
                    "pivot_price":            r["pivot_price"],
                    "suggested_stop":         r["suggested_stop"],
                    "current_price_vs_pivot": r["current_price_vs_pivot"],
                    "tightness_pct":          r["tightness_pct"],
                    "close":                  r.get("close"),
                })
            except Exception:
                pass


_SCANNERS = [_cup_handle, _golden_cross, _ma200_breakout, _pullback_bounce,
             _momentum_expansion, _momentum_10_8]


def scan_date_range(from_date: str, to_date: str, progress_cb=None,
                     squeeze_threshold: float = None, price_threshold: float = None,
                     max_squeeze_age: float = None, vol_surge_mult: float = None) -> list:
    """Scan all patterns for every trading day in [from_date, to_date].
    Each date's results are saved to the DB independently (same as single-date scan).
    """
    sq_thresh  = squeeze_threshold if squeeze_threshold is not None else SQUEEZE_THRESHOLD
    pr_thresh  = price_threshold   if price_threshold   is not None else PRICE_THRESHOLD
    age_thresh = max_squeeze_age   if max_squeeze_age   is not None else MAX_SQUEEZE_AGE
    vs_mult    = vol_surge_mult    if vol_surge_mult    is not None else VOL_SURGE_MULT
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
        vcp_results  = []
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
            try:
                r = _volume_surge(ticker, rows_up_to, vs_mult)
                if r:
                    date_results.append({**r, "scan_date": scan_date})
            except Exception:
                pass
            try:
                r = _ma_squeeze(ticker, rows_up_to, sq_thresh, pr_thresh, age_thresh)
                if r:
                    date_results.append({**r, "scan_date": scan_date})
            except Exception:
                pass
            vcp_sig, vcp_pat = _run_vcp(ticker, rows_up_to)
            if vcp_sig:
                vcp_results.append(vcp_sig)
            if vcp_pat:
                date_results.append({**vcp_pat, "scan_date": scan_date})
        _save_results(scan_date, date_results)
        _save_vcp_results(scan_date, vcp_results)
        all_results.extend(date_results)

    return all_results


def scan_all_patterns(scan_date: str = None, progress_cb=None,
                       squeeze_threshold: float = None, price_threshold: float = None,
                       max_squeeze_age: float = None, vol_surge_mult: float = None) -> list:
    if not scan_date:
        scan_date = datetime.now().strftime("%Y-%m-%d")
    sq_thresh  = squeeze_threshold if squeeze_threshold is not None else SQUEEZE_THRESHOLD
    pr_thresh  = price_threshold   if price_threshold   is not None else PRICE_THRESHOLD
    age_thresh = max_squeeze_age   if max_squeeze_age   is not None else MAX_SQUEEZE_AGE
    vs_mult    = vol_surge_mult    if vol_surge_mult    is not None else VOL_SURGE_MULT
    all_prices  = _load_prices_up_to(scan_date)
    total       = len(all_prices)
    results     = []
    vcp_results = []
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
        try:
            r = _volume_surge(ticker, rows, vs_mult)
            if r:
                results.append({**r, "scan_date": scan_date})
        except Exception:
            pass
        try:
            r = _ma_squeeze(ticker, rows, sq_thresh, pr_thresh, age_thresh)
            if r:
                results.append({**r, "scan_date": scan_date})
        except Exception:
            pass
        vcp_sig, vcp_pat = _run_vcp(ticker, rows)
        if vcp_sig:
            vcp_results.append(vcp_sig)
        if vcp_pat:
            results.append({**vcp_pat, "scan_date": scan_date})
    _save_results(scan_date, results)
    _save_vcp_results(scan_date, vcp_results)
    return results


def _save_results(scan_date: str, results: list):
    with get_connection() as conn:
        conn.execute("DELETE FROM pattern_scan_results WHERE scan_date = ?", (scan_date,))
        for r in results:
            try:
                r.setdefault("mom_15d_start", None)
                r.setdefault("mom_15d_gain_pct", None)
                r.setdefault("squeeze_spread_pct", None)
                r.setdefault("squeeze_days", None)
                r.setdefault("squeeze_fresh", None)
                conn.execute("""
                    INSERT INTO pattern_scan_results
                        (scan_date, ticker, pattern_name, signal_detail,
                         signal_date, close, ma9, ma21, ma30, ma50, ma100, ma200,
                         volume, vol_ma10, high_30d, low_30d, pct_change,
                         mom_15d_start, mom_15d_gain_pct,
                         squeeze_spread_pct, squeeze_days, squeeze_fresh)
                    VALUES
                        (:scan_date, :ticker, :pattern_name, :signal_detail,
                         :signal_date, :close, :ma9, :ma21, :ma30, :ma50, :ma100, :ma200,
                         :volume, :vol_ma10, :high_30d, :low_30d, :pct_change,
                         :mom_15d_start, :mom_15d_gain_pct,
                         :squeeze_spread_pct, :squeeze_days, :squeeze_fresh)
                """, r)
            except Exception:
                pass


_TT_JOIN = """
    LEFT JOIN stocks_daily s
           ON s.ticker = p.ticker
          AND s.date = (
                SELECT MAX(s2.date) FROM stocks_daily s2
                WHERE s2.ticker = p.ticker AND s2.date <= p.scan_date
              )
    LEFT JOIN universe_prices u
           ON u.ticker = p.ticker
          AND s.ticker IS NULL
          AND u.date = (
                SELECT MAX(u2.date) FROM universe_prices u2
                WHERE u2.ticker = p.ticker AND u2.date <= p.scan_date
              )
    LEFT JOIN vcp_signals  v  ON v.ticker = p.ticker AND v.scan_date = p.scan_date
    LEFT JOIN rs_ratings   rr ON rr.ticker = p.ticker AND rr.date = COALESCE(s.date, u.date)
"""
# stocks_daily (s) wins when a ticker is tracked; universe_prices (u) fills in
# the Trend Template/RS/52wk columns for the ~787 active-universe tickers that
# aren't on the tracked Ticker List (same "tracked wins on overlap" rule
# /api/data/summary uses) — _load_prices_up_to() applies the same rule for
# the pattern DETECTION side, this is the display-join side.
_TT_COLS = """
    COALESCE(s.ma150, u.ma150) AS ma150,
    COALESCE(s.high_52wk, u.high_52wk) AS high_52wk,
    COALESCE(s.low_52wk, u.low_52wk) AS low_52wk,
    COALESCE(rr.rs_rating, s.rs_rank, u.rs_rank) AS rs_rank,
    rr.rs_leader,
    COALESCE(s.c1, u.c1) AS c1, COALESCE(s.c2, u.c2) AS c2, COALESCE(s.c3, u.c3) AS c3,
    COALESCE(s.c4, u.c4) AS c4, COALESCE(s.c5, u.c5) AS c5, COALESCE(s.c6, u.c6) AS c6,
    COALESCE(s.c7, u.c7) AS c7, COALESCE(s.c8, u.c8) AS c8,
    COALESCE(s.trend_score, u.trend_score) AS trend_score,
    v.vcp_detected, v.num_contractions, v.contraction_depths, v.volume_dryup,
    v.pivot_price, v.suggested_stop, v.current_price_vs_pivot, v.tightness_pct
"""


def _rows_with_parsed_vcp(rows: list) -> list:
    out = []
    for r in rows:
        d = dict(r)
        if d.get("contraction_depths"):
            try:
                d["contraction_depths"] = json.loads(d["contraction_depths"])
            except (TypeError, ValueError):
                d["contraction_depths"] = []
        out.append(d)
    return out


def get_scan_results_range(from_date: str, to_date: str) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT p.*, {_TT_COLS}
               FROM pattern_scan_results p
               {_TT_JOIN}
               WHERE p.scan_date >= ? AND p.scan_date <= ?
               ORDER BY p.scan_date DESC, p.pattern_name, p.ticker""",
            (from_date, to_date),
        ).fetchall()
    return _rows_with_parsed_vcp(rows)


def get_scan_results(scan_date: str) -> list:
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT p.*, {_TT_COLS}
               FROM pattern_scan_results p
               {_TT_JOIN}
               WHERE p.scan_date = ?
               ORDER BY p.pattern_name, p.ticker""",
            (scan_date,)
        ).fetchall()
    return _rows_with_parsed_vcp(rows)


def get_available_scan_dates() -> list:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT scan_date, COUNT(*) AS count
               FROM pattern_scan_results
               GROUP BY scan_date
               ORDER BY scan_date DESC"""
        ).fetchall()
    return [dict(r) for r in rows]
