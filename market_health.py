"""
market_health.py — IBD-style Distribution Day / Follow-Through Day tracker

Distribution Days (DD):
  Close falls DD_PCT_DROP or more  AND  volume > prior-day volume.
  Rolling DD_WINDOW trading-day window; early removal if the index closes
  RECOVERY_PCT above the DD's closing price at any point afterwards.

Follow-Through Days (FTD):
  On day FTD_START_DAY or later of an attempted rally (counted from the
  most-recent low), the index closes up FTD_PCT_GAIN or more with
  volume higher than the prior day.

Market Status thresholds (higher of Nasdaq / S&P 500 DD counts):
  0–3  DDs  →  Healthy / Confirmed Uptrend
  4–5  DDs  →  Caution — Market Under Pressure
  6+   DDs  →  Correction Likely / Defensive
  6+   DDs + recent FTD  →  New Uptrend Confirmed (FTD override)

Run standalone for a quick sanity-check:
  python market_health.py
"""

import yfinance as yf
import pandas as pd
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configurable thresholds — tune here without touching logic below
# ---------------------------------------------------------------------------

SYMBOLS      = ["^IXIC", "^GSPC"]   # Nasdaq Composite, S&P 500
DD_PCT_DROP  = -0.002               # −0.2 %: min daily decline for a DD
FTD_PCT_GAIN =  0.0125             # +1.25 %: min daily gain for a FTD
DD_WINDOW    = 25                   # rolling trading-day window for DD count
RECOVERY_PCT =  0.05               # 5 % gain from DD close removes that DD early
FETCH_PERIOD = "90d"               # ~65 trading days + buffer; adjust if needed
FTD_START_DAY = 4                  # earliest rally day that qualifies for a FTD
FTD_LOOKBACK  = 10                 # flag a FTD "recent" only if within this many trading days


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct(a, b):
    """Fractional return (a − b) / b.  Returns 0.0 if b is zero or falsy."""
    return float((a - b) / b) if b else 0.0


def _safe_float(v):
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 1. Data fetch
# ---------------------------------------------------------------------------

def fetch_index_data(symbols=None, period=FETCH_PERIOD):
    """
    Download daily OHLCV for each index symbol via yfinance.
    Returns dict: symbol → DataFrame (ascending date, tz-naive index).
    Silently skips any symbol that returns empty data.
    """
    if symbols is None:
        symbols = SYMBOLS
    result = {}
    for sym in symbols:
        try:
            df = yf.Ticker(sym).history(period=period)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        df = df.copy()
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df.sort_index()
        df.index = pd.to_datetime(df.index)
        result[sym] = df
    return result


# ---------------------------------------------------------------------------
# 2. Distribution Day detection
# ---------------------------------------------------------------------------

def detect_distribution_days(df):
    """
    Scan df and return a list of distribution-day dicts.
    Each dict carries a private '_date_obj' key (datetime) used by apply_expiry_rules.

    Criteria: pct_change <= DD_PCT_DROP  AND  volume[t] > volume[t-1]
    """
    dist_days = []
    dates   = df.index.tolist()
    closes  = df["Close"].tolist()
    volumes = df["Volume"].tolist()

    for i in range(1, len(dates)):
        prev_c = _safe_float(closes[i - 1])
        cur_c  = _safe_float(closes[i])
        prev_v = volumes[i - 1]
        cur_v  = volumes[i]

        if prev_c is None or cur_c is None or not prev_c or not prev_v:
            continue

        pct = _pct(cur_c, prev_c)

        if pct <= DD_PCT_DROP and cur_v > prev_v:
            dist_days.append({
                "date":         dates[i].strftime("%Y-%m-%d"),
                "close":        round(cur_c, 4),
                "pct_change":   round(pct * 100, 4),
                "volume":       int(cur_v),
                "prior_volume": int(prev_v),
                "vol_ratio":    round(float(cur_v) / float(prev_v), 2),
                "_date_obj":    dates[i],          # internal only
            })
    return dist_days


# ---------------------------------------------------------------------------
# 3. Expiry / removal rules
# ---------------------------------------------------------------------------

def apply_expiry_rules(dist_days, df):
    """
    Filter out distribution days that have expired:
      Rule 1: older than DD_WINDOW trading days from the last data row.
      Rule 2: the index has closed RECOVERY_PCT or more above that DD's close
              at any point since the DD date.

    Adds 'days_remaining' (int) to each surviving day and strips '_date_obj'.
    """
    if not dist_days:
        return []

    all_dates  = df.index.tolist()
    all_closes = {d: _safe_float(c) for d, c in zip(all_dates, df["Close"].tolist())}
    today_idx  = len(all_dates) - 1
    d_to_idx   = {d: i for i, d in enumerate(all_dates)}

    active = []
    for dd in dist_days:
        dd_date = dd["_date_obj"]
        dd_idx  = d_to_idx.get(dd_date)
        if dd_idx is None:
            continue

        trading_days_ago = today_idx - dd_idx

        # Rule 1: outside rolling window
        if trading_days_ago >= DD_WINDOW:
            continue

        # Rule 2: 5 %+ recovery since the DD
        dd_close  = dd["close"]
        recovered = any(
            (all_closes.get(fd) or 0) > 0
            and _pct(all_closes[fd], dd_close) >= RECOVERY_PCT
            for fd in all_dates[dd_idx + 1:]
        )
        if recovered:
            continue

        active.append({
            **{k: v for k, v in dd.items() if k != "_date_obj"},
            "days_remaining": int(DD_WINDOW - trading_days_ago),
        })

    return active


# ---------------------------------------------------------------------------
# 4. Follow-Through Day detection
# ---------------------------------------------------------------------------

def detect_follow_through_days(df):
    """
    Walk through df tracking an "attempted rally" from the most-recent low.
    A new low resets the rally counter.  On day FTD_START_DAY or later:
      close up FTD_PCT_GAIN+  AND  volume > prior-day volume  →  FTD.

    Returns a chronological list of FTD dicts.
    """
    dates   = df.index.tolist()
    closes  = df["Close"].tolist()
    volumes = df["Volume"].tolist()
    n       = len(dates)
    if n < FTD_START_DAY + 1:
        return []

    ftds        = []
    rally_low   = _safe_float(closes[0]) or 0.0
    rally_start = 0                        # index of the current low

    for i in range(1, n):
        c   = _safe_float(closes[i])
        c_p = _safe_float(closes[i - 1])
        v   = volumes[i]
        v_p = volumes[i - 1]

        if c is None or c_p is None:
            continue

        # New low → reset the rally attempt
        if c < rally_low:
            rally_low   = c
            rally_start = i
            continue

        rally_day = i - rally_start
        if rally_day < FTD_START_DAY:
            continue

        pct = _pct(c, c_p)
        if pct >= FTD_PCT_GAIN and v_p > 0 and int(v) > int(v_p):
            ftds.append({
                "date":         dates[i].strftime("%Y-%m-%d"),
                "close":        round(c, 4),
                "pct_change":   round(pct * 100, 4),
                "volume":       int(v),
                "prior_volume": int(v_p),
                "rally_day":    rally_day,
            })

    return ftds


# ---------------------------------------------------------------------------
# 5. Market status classification
# ---------------------------------------------------------------------------

def classify_market_status(ixic_count, gspc_count, recent_ftd=None):
    """
    Return a status dict based on the higher of the two DD counts.
    FTD override applies only when dd_max >= 6 (correction territory).
    """
    dd_max = max(ixic_count, gspc_count)

    if recent_ftd and dd_max >= 6:
        return {"status": "recovery",   "label": "New Uptrend Confirmed (FTD)",     "color": "green",  "dd_max": dd_max}
    if dd_max <= 3:
        return {"status": "healthy",    "label": "Healthy / Confirmed Uptrend",      "color": "green",  "dd_max": dd_max}
    if dd_max <= 5:
        return {"status": "caution",    "label": "Caution — Market Under Pressure",  "color": "yellow", "dd_max": dd_max}
    return     {"status": "correction", "label": "Correction Likely / Defensive",    "color": "red",    "dd_max": dd_max}


# ---------------------------------------------------------------------------
# 6. Main entry point
# ---------------------------------------------------------------------------

def get_market_health():
    """
    Orchestrate data fetch → DD detection → expiry rules → FTD detection
    → status classification.

    Returns a dict ready for JSON serialisation (no numpy/pandas types).
    """
    index_data = fetch_index_data()

    result = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "indices":     {},
        "status":      None,
        "recent_ftd":  None,
        "chart_data":  {},
    }

    ftd_candidates = []   # (ftd_dict, trading_days_from_end_of_data)

    for sym in SYMBOLS:
        df = index_data.get(sym)
        if df is None or df.empty:
            result["indices"][sym] = {
                "error": "no data", "dd_count": 0, "dd_list": [], "ftd_list": []
            }
            continue

        raw_dds    = detect_distribution_days(df)
        active_dds = apply_expiry_rules(raw_dds, df)
        ftds       = detect_follow_through_days(df)

        # Build trading-day distance for each FTD so we can pick the "recent" one
        dates    = df.index.tolist()
        n        = len(dates)
        d_to_idx = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(dates)}
        for ftd in ftds:
            idx = d_to_idx.get(ftd["date"], -1)
            if idx >= 0:
                ftd_candidates.append((ftd, n - 1 - idx))

        result["indices"][sym] = {
            "dd_count":  len(active_dds),
            "dd_list":   active_dds,
            "latest_dd": active_dds[-1] if active_dds else None,
            "ftd_list":  ftds,
        }

        # Chart data: last 60 trading days (closes + marker date sets)
        df_60      = df.tail(60)
        dd_set     = {dd["date"] for dd in active_dds}
        ftd_set    = {f["date"]  for f in ftds}
        chart_dates = [d.strftime("%Y-%m-%d") for d in df_60.index]

        result["chart_data"][sym] = {
            "dates":     chart_dates,
            "closes":    [round(float(c), 4) for c in df_60["Close"].tolist()
                         if _safe_float(c) is not None] or [],
            "dd_dates":  [d for d in chart_dates if d in dd_set],
            "ftd_dates": [d for d in chart_dates if d in ftd_set],
        }
        # Ensure closes list length matches dates length (fill None → 0)
        result["chart_data"][sym]["closes"] = [
            round(float(c), 4) if _safe_float(c) is not None else 0.0
            for c in df_60["Close"].tolist()
        ]

    # Most recent FTD within FTD_LOOKBACK trading days of the last data point
    recent_ftd = None
    for ftd, days_from_end in sorted(ftd_candidates, key=lambda x: x[0]["date"], reverse=True):
        if days_from_end <= FTD_LOOKBACK:
            recent_ftd = ftd
            break
    result["recent_ftd"] = recent_ftd

    ixic_count = result["indices"].get("^IXIC", {}).get("dd_count", 0)
    gspc_count = result["indices"].get("^GSPC", {}).get("dd_count", 0)
    result["status"] = classify_market_status(ixic_count, gspc_count, recent_ftd)

    return result


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    print("Fetching market health data…")
    health = get_market_health()
    print(json.dumps(health, indent=2, default=str))
