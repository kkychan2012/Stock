"""
VCP (Volatility Contraction Pattern) detector — Minervini-style scanner.

Looks for a series of progressively smaller pullbacks (contractions) within an
uptrend, ending in a tight, low-volume consolidation near a pivot point.

Reuses the Trend Template score (c1-c8 / trend_score) already computed by
fetch_data.py as a prerequisite filter, rather than re-deriving trend criteria.
Swing highs/lows are found with a simple N-day pivot window (no scipy dependency
needed — the project doesn't otherwise use it).
"""

# ── Tuneable config ────────────────────────────────────────────────────────────
VCP_LOOKBACK_DAYS         = 252   # ~12 months of daily history to analyze
VCP_PIVOT_WINDOW          = 5     # N-day window for swing high/low detection
VCP_MIN_CONTRACTIONS      = 2     # minimum contracting legs required to flag VCP
VCP_MAX_LEGS              = 3     # keep only the most recent N pullback legs (2-4)
VCP_CONTRACTION_TOLERANCE = 0.20  # each leg's depth must be ≥20% smaller than the prior one
VCP_TIGHTNESS_DAYS        = 10    # final N days used for the tightness / pivot check
VCP_TIGHTNESS_MAX_PCT     = 5.0   # "tight" if last N-day range ≤ 5% of price
VCP_VOLUME_DRYUP_RATIO    = 1.0   # final leg's avg volume must be < this × trailing 50D avg volume
VCP_STOP_PCT              = 7.0   # suggested stop = pivot × (1 - 7%)
VCP_MIN_TREND_SCORE       = 6     # prerequisite: Trend Template score out of 8 ("mostly passes")
VCP_MIN_ROWS              = 60    # minimum history required before attempting detection

DEFAULT_CONFIG = {
    "lookback_days":         VCP_LOOKBACK_DAYS,
    "pivot_window":          VCP_PIVOT_WINDOW,
    "min_contractions":      VCP_MIN_CONTRACTIONS,
    "max_legs":              VCP_MAX_LEGS,
    "contraction_tolerance": VCP_CONTRACTION_TOLERANCE,
    "tightness_days":        VCP_TIGHTNESS_DAYS,
    "tightness_max_pct":     VCP_TIGHTNESS_MAX_PCT,
    "volume_dryup_ratio":    VCP_VOLUME_DRYUP_RATIO,
    "stop_pct":              VCP_STOP_PCT,
    "min_trend_score":       VCP_MIN_TREND_SCORE,
}
# ──────────────────────────────────────────────────────────────────────────────


def _empty_result(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "vcp_detected": False,
        "num_contractions": 0,
        "contraction_depths": [],
        "volume_dryup": False,
        "pivot_price": None,
        "suggested_stop": None,
        "current_price_vs_pivot": None,
        "tightness_pct": None,
    }


def _find_swing_points(rows: list, window: int) -> list:
    """N-day pivot detection: row i is a swing high if its high is the max within
    [i-window, i+window]; a swing low if its low is the min in that same range."""
    points = []
    n = len(rows)
    for i in range(window, n - window):
        seg   = rows[i - window: i + window + 1]
        highs = [r["high"] for r in seg if r.get("high") is not None]
        lows  = [r["low"]  for r in seg if r.get("low")  is not None]
        hi, lo = rows[i].get("high"), rows[i].get("low")
        if hi is not None and highs and hi == max(highs):
            points.append((i, "H", hi, rows[i]["date"]))
        if lo is not None and lows and lo == min(lows):
            points.append((i, "L", lo, rows[i]["date"]))
    return points


def _alternate(points: list) -> list:
    """Collapse consecutive same-type swing points to the most extreme one, so the
    sequence strictly alternates H/L/H/L..."""
    if not points:
        return []
    seq = [points[0]]
    for p in points[1:]:
        if p[1] == seq[-1][1]:
            if p[1] == "H" and p[2] > seq[-1][2]:
                seq[-1] = p
            elif p[1] == "L" and p[2] < seq[-1][2]:
                seq[-1] = p
        else:
            seq.append(p)
    return seq


def _build_legs(rows: list, alt_points: list) -> list:
    """Extract peak→trough pullback legs from an alternating swing-point sequence."""
    legs = []
    for a, b in zip(alt_points, alt_points[1:]):
        if a[1] != "H" or b[1] != "L":
            continue
        peak_idx, _, peak_price, peak_date     = a
        trough_idx, _, trough_price, trough_date = b
        if not peak_price:
            continue
        seg  = rows[peak_idx: trough_idx + 1]
        vols = [r["volume"] for r in seg if r.get("volume") is not None]
        legs.append({
            "peak_date":   peak_date,
            "peak_price":  peak_price,
            "trough_date": trough_date,
            "trough_price": trough_price,
            "depth_pct":   (peak_price - trough_price) / peak_price * 100,
            "avg_volume":  sum(vols) / len(vols) if vols else None,
        })
    return legs


def _contraction_chain(legs: list, tolerance: float) -> list:
    """Walk backward from the most recent leg, keeping earlier legs only while each
    one's depth is at least `tolerance` bigger than the leg that follows it."""
    if not legs:
        return []
    chain = [legs[-1]]
    for leg in reversed(legs[:-1]):
        later = chain[0]
        if later["depth_pct"] <= leg["depth_pct"] * (1 - tolerance):
            chain.insert(0, leg)
        else:
            break
    return chain


def _volume_dryup(chain: list, rows: list, ratio_thresh: float) -> bool:
    vols = [leg["avg_volume"] for leg in chain if leg["avg_volume"] is not None]
    if len(vols) < 2:
        return False
    decreasing = all(vols[i] <= vols[i - 1] for i in range(1, len(vols)))

    last_50 = rows[-50:] if len(rows) >= 50 else rows
    vol50   = [r["volume"] for r in last_50 if r.get("volume") is not None]
    avg50   = sum(vol50) / len(vol50) if vol50 else None
    final_ok = avg50 is not None and vols[-1] <= avg50 * ratio_thresh

    return decreasing and final_ok


def _tightness_and_pivot(rows: list, n_days: int):
    seg   = rows[-n_days:] if len(rows) >= n_days else rows
    highs = [r["high"] for r in seg if r.get("high") is not None]
    lows  = [r["low"]  for r in seg if r.get("low")  is not None]
    if not highs or not lows:
        return None, None
    pivot      = max(highs)
    last_close = rows[-1].get("close")
    if not last_close:
        return None, pivot
    tightness_pct = (max(highs) - min(lows)) / last_close * 100
    return tightness_pct, pivot


def detect_vcp(ticker: str, rows: list, config: dict = None) -> dict:
    """Detect a Volatility Contraction Pattern for one ticker.

    rows: price history sorted ascending by date, each a dict with at least
    date, close, high, low, volume — plus trend_score if available (used as the
    Trend Template prerequisite filter; rows without it skip that filter rather
    than fail it, since not every history has it backfilled).
    config: optional overrides merged onto DEFAULT_CONFIG.
    """
    cfg    = {**DEFAULT_CONFIG, **(config or {})}
    result = _empty_result(ticker)

    if not rows or len(rows) < max(cfg["pivot_window"] * 2 + 1, VCP_MIN_ROWS):
        return result

    last        = rows[-1]
    trend_score = last.get("trend_score")
    if trend_score is not None and trend_score < cfg["min_trend_score"]:
        return result

    window_rows = rows[-cfg["lookback_days"]:] if len(rows) > cfg["lookback_days"] else rows

    points = _find_swing_points(window_rows, cfg["pivot_window"])
    alt    = _alternate(points)
    legs   = _build_legs(window_rows, alt)
    if len(legs) < cfg["min_contractions"]:
        return result

    recent_legs = legs[-cfg["max_legs"]:]
    chain       = _contraction_chain(recent_legs, cfg["contraction_tolerance"])

    tightness_pct, pivot = _tightness_and_pivot(window_rows, cfg["tightness_days"])
    close = last.get("close")

    result["contraction_depths"] = [round(l["depth_pct"], 2) for l in chain]
    result["num_contractions"]   = len(chain)
    result["tightness_pct"]      = round(tightness_pct, 2) if tightness_pct is not None else None
    result["volume_dryup"]       = _volume_dryup(chain, window_rows, cfg["volume_dryup_ratio"])

    if pivot:
        result["pivot_price"]    = round(pivot, 4)
        result["suggested_stop"] = round(pivot * (1 - cfg["stop_pct"] / 100), 4)
        if close:
            result["current_price_vs_pivot"] = round((close - pivot) / pivot * 100, 2)

    result["vcp_detected"] = len(chain) >= cfg["min_contractions"]
    return result
