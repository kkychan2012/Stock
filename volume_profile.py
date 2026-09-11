"""
Volume Profile module.

For a ticker's price history over a lookback window (default 60 trading
days), builds a volume-by-price histogram, reports the POC (point of
control — the single price level with the most traded volume) and the top N
high-volume price levels, and classifies each level as support (at/below the
current price) or resistance (above it).

Those volume-derived levels are then cross-referenced against pivot-based
support/resistance levels — clusters of swing high/low "pivot" points that
price has repeatedly reversed at, the classic "S/R channel" technique. There
was no existing pivot-based S/R module anywhere in this codebase to
cross-reference against (grepped for SRchannel/pivot/support/resistance —
nothing), so `pivot_levels()` below is a new companion detector. It reuses
the same N-day swing-high/low pivot window already established in
vcp_detector.py, but — unlike that module, which chains peak→trough pullback
legs specifically for VCP — clusters ALL swing points (highs and lows alike)
into standing price bands by proximity, which is the general-purpose
"pivot S/R" shape.

Levels that show up in BOTH the volume top-N and a pivot cluster (within
`confirm_tolerance_pct` of each other) are flagged `confirmed_by_pivot` and
surfaced in `confirmed_levels` — the highest-confidence support/resistance
this module produces.

`analyze()` is the single entry point, mirroring vcp_detector.py's
`detect_vcp(ticker, rows, config=None)` shape: `rows` is the same ascending
list-of-dicts (date/open/high/low/close/volume) used across scan_patterns.py.
"""

# ── Tuneable config ────────────────────────────────────────────────────────────
VP_LOOKBACK_DAYS         = 60    # trading days of history used for the volume histogram
VP_NUM_BINS              = 24    # equal-width price bins spanning the lookback's low..high range
VP_TOP_N                 = 5     # top N high-volume price levels to return

VP_PIVOT_LOOKBACK_DAYS   = 120   # longer window for pivot-based S/R — swing points need room to form
VP_PIVOT_WINDOW          = 5     # N-day window for swing high/low detection (same technique as vcp_detector.py)
VP_PIVOT_CLUSTER_PCT     = 1.5   # swing points within this % of each other merge into one S/R channel
VP_PIVOT_MIN_TOUCHES     = 2     # a pivot channel needs at least this many swing-point touches to count

VP_CONFIRM_TOLERANCE_PCT = 1.5   # max % distance between a volume level and a pivot level to call them the same level

DEFAULT_CONFIG = {
    "lookback_days":         VP_LOOKBACK_DAYS,
    "num_bins":              VP_NUM_BINS,
    "top_n":                 VP_TOP_N,
    "pivot_lookback_days":   VP_PIVOT_LOOKBACK_DAYS,
    "pivot_window":          VP_PIVOT_WINDOW,
    "pivot_cluster_pct":     VP_PIVOT_CLUSTER_PCT,
    "pivot_min_touches":     VP_PIVOT_MIN_TOUCHES,
    "confirm_tolerance_pct": VP_CONFIRM_TOLERANCE_PCT,
}
# ──────────────────────────────────────────────────────────────────────────────


def _empty_result(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "current_price": None,
        "lookback_days": DEFAULT_CONFIG["lookback_days"],
        "poc": None,
        "volume_levels": [],
        "pivot_levels": [],
        "confirmed_levels": [],
    }


def _classify(price: float, current_price: float) -> str:
    """Below (or at) current price = support; above = resistance."""
    return "support" if price <= current_price else "resistance"


def _build_bins(rows: list, num_bins: int) -> list:
    """Distributes each day's volume across the price bins its [low, high]
    range overlaps, proportional to the overlap width — a day with a tight
    range puts most/all of its volume in one bin, a wide-range day spreads
    across several. This is the standard no-tick-data approximation for a
    volume profile (true tick-level profiles aren't available from daily
    OHLCV)."""
    highs = [r["high"] for r in rows if r.get("high") is not None]
    lows  = [r["low"]  for r in rows if r.get("low")  is not None]
    if not highs or not lows:
        return []
    hi, lo = max(highs), min(lows)
    if hi <= lo:
        return []
    width = (hi - lo) / num_bins
    bins = [{
        "index":      i,
        "price_low":  lo + i * width,
        "price_high": lo + (i + 1) * width,
        "mid":        lo + (i + 0.5) * width,
        "volume":     0.0,
    } for i in range(num_bins)]

    for r in rows:
        vol, rl, rh = r.get("volume"), r.get("low"), r.get("high")
        if vol is None or rl is None or rh is None:
            continue
        if rh <= rl:
            # no intraday range on this row — dump its whole volume in the bin that contains it
            idx = min(max(int((rl - lo) / width), 0), num_bins - 1)
            bins[idx]["volume"] += vol
            continue
        span = rh - rl
        lo_idx = max(int((rl - lo) / width), 0)
        hi_idx = min(int((rh - lo) / width), num_bins - 1)
        for i in range(lo_idx, hi_idx + 1):
            b = bins[i]
            overlap = min(b["price_high"], rh) - max(b["price_low"], rl)
            if overlap > 0:
                b["volume"] += vol * (overlap / span)
    return bins


def _merge_adjacent_bins(sorted_bins: list) -> list:
    """Greedily merges index-adjacent bins into one level so a single volume
    peak that straddles a bin boundary isn't reported as two separate levels.
    `sorted_bins` must already be sorted descending by volume."""
    levels = []
    for b in sorted_bins:
        idx = b["index"]
        target = next((lvl for lvl in levels
                        if idx - 1 in lvl["_indices"] or idx + 1 in lvl["_indices"]), None)
        if target is not None:
            target["_indices"].append(idx)
            target["volume"] += b["volume"]
            target["_wsum"]  += b["volume"] * b["mid"]
            target["price"]   = target["_wsum"] / target["volume"] if target["volume"] else b["mid"]
            target["price_low"]  = min(target["price_low"], b["price_low"])
            target["price_high"] = max(target["price_high"], b["price_high"])
        else:
            levels.append({
                "_indices":   [idx],
                "_wsum":      b["volume"] * b["mid"],
                "volume":     b["volume"],
                "price":      b["mid"],
                "price_low":  b["price_low"],
                "price_high": b["price_high"],
            })
    levels.sort(key=lambda l: l["volume"], reverse=True)
    for lvl in levels:
        del lvl["_indices"], lvl["_wsum"]
    return levels


def volume_levels(rows: list, num_bins: int, top_n: int, current_price: float):
    """Returns (poc_price, [top N high-volume levels]), each level classified
    support/resistance relative to current_price."""
    bins = _build_bins(rows, num_bins)
    if not bins:
        return None, []

    poc_bin = max(bins, key=lambda b: b["volume"])
    poc_price = round(poc_bin["mid"], 4) if poc_bin["volume"] > 0 else None

    ranked = sorted((b for b in bins if b["volume"] > 0), key=lambda b: b["volume"], reverse=True)
    merged = _merge_adjacent_bins(ranked)[:top_n]
    total_vol = sum(b["volume"] for b in bins) or 1

    out = []
    for lvl in merged:
        price = round(lvl["price"], 4)
        out.append({
            "price":               price,
            "price_low":           round(lvl["price_low"], 4),
            "price_high":          round(lvl["price_high"], 4),
            "volume":              round(lvl["volume"], 2),
            "pct_of_total_volume": round(lvl["volume"] / total_vol * 100, 2),
            "type":                _classify(price, current_price),
            "is_poc":              poc_price is not None and lvl["price_low"] <= poc_price <= lvl["price_high"],
            "confirmed_by_pivot":  False,   # filled in by cross_reference()
            "matched_pivot":       None,
            "confidence":          "volume_only",
        })
    return poc_price, out


def _find_swing_points(rows: list, window: int) -> list:
    """Same N-day pivot technique as vcp_detector.py's `_find_swing_points`
    (row i is a swing high/low if its high/low is the extreme within
    [i-window, i+window]), kept local here rather than imported: that version
    tags points with index/type/date for leg-building, this one only needs
    the bare prices for clustering."""
    points = []
    n = len(rows)
    for i in range(window, n - window):
        seg   = rows[i - window: i + window + 1]
        highs = [r["high"] for r in seg if r.get("high") is not None]
        lows  = [r["low"]  for r in seg if r.get("low")  is not None]
        hi, lo = rows[i].get("high"), rows[i].get("low")
        if hi is not None and highs and hi == max(highs):
            points.append(hi)
        if lo is not None and lows and lo == min(lows):
            points.append(lo)
    return points


def pivot_levels(rows: list, window: int, cluster_pct: float, min_touches: int, current_price: float) -> list:
    """Clusters swing high/low pivot points into 'S/R channels': price bands
    where the stock has repeatedly reversed. Strength = touch count. This is
    the pivot-based counterpart the volume levels get cross-referenced
    against — highs and lows are clustered together since a level can act as
    either support or resistance depending on which side price approaches
    from (classic role-reversal), and its classification here is simply
    relative to the current price, same as the volume levels."""
    points = sorted(_find_swing_points(rows, window))
    if not points:
        return []

    clusters = []
    for p in points:
        if clusters and clusters[-1]["mean"] and abs(p - clusters[-1]["mean"]) / clusters[-1]["mean"] * 100 <= cluster_pct:
            c = clusters[-1]
            c["touches"] += 1
            c["sum"]     += p
            c["mean"]     = c["sum"] / c["touches"]
            c["min"]      = min(c["min"], p)
            c["max"]      = max(c["max"], p)
        else:
            clusters.append({"touches": 1, "sum": p, "mean": p, "min": p, "max": p})

    out = []
    for c in clusters:
        if c["touches"] < min_touches:
            continue
        price = round(c["mean"], 4)
        out.append({
            "price":      price,
            "price_low":  round(c["min"], 4),
            "price_high": round(c["max"], 4),
            "touches":    c["touches"],
            "type":       _classify(price, current_price),
        })
    out.sort(key=lambda l: l["touches"], reverse=True)
    return out


def cross_reference(vol_levels: list, piv_levels: list, tolerance_pct: float):
    """Mutates each volume level in place with confirmed_by_pivot/matched_pivot/
    confidence, and returns (vol_levels, confirmed_levels) — the subset flagged
    as highest-confidence because BOTH a volume peak and a pivot-touch cluster
    landed on (near) the same price."""
    confirmed = []
    for vl in vol_levels:
        best = None
        for pl in piv_levels:
            base = pl["price"] or vl["price"]
            if not base:
                continue
            pct_diff = abs(vl["price"] - pl["price"]) / base * 100
            if pct_diff <= tolerance_pct and (best is None or pl["touches"] > best["touches"]):
                best = pl
        if best is not None:
            vl["confirmed_by_pivot"] = True
            vl["matched_pivot"] = {"price": best["price"], "touches": best["touches"]}
            vl["confidence"] = "high"
            confirmed.append(vl)
    return vol_levels, confirmed


def analyze(ticker: str, rows: list, config: dict = None) -> dict:
    """Main entry point. `rows`: price history sorted ascending by date, each a
    dict with at least date, close, high, low, volume — same shape used across
    scan_patterns.py / vcp_detector.py. Needs enough rows to cover
    max(lookback_days, pivot_lookback_days) + pivot_window for the pivot side
    to have any swing points near its edges; shorter history just yields fewer
    (or no) pivot levels rather than erroring.
    config: optional overrides merged onto DEFAULT_CONFIG.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    result = _empty_result(ticker)
    result["lookback_days"] = cfg["lookback_days"]
    if not rows:
        return result

    current_price = rows[-1].get("close")
    result["current_price"] = current_price
    if current_price is None:
        return result

    vol_window = rows[-cfg["lookback_days"]:] if len(rows) > cfg["lookback_days"] else rows
    poc, vlevels = volume_levels(vol_window, cfg["num_bins"], cfg["top_n"], current_price)
    result["poc"] = poc

    piv_window = rows[-cfg["pivot_lookback_days"]:] if len(rows) > cfg["pivot_lookback_days"] else rows
    plevels = pivot_levels(piv_window, cfg["pivot_window"], cfg["pivot_cluster_pct"],
                            cfg["pivot_min_touches"], current_price)
    result["pivot_levels"] = plevels

    vlevels, confirmed = cross_reference(vlevels, plevels, cfg["confirm_tolerance_pct"])
    result["volume_levels"] = vlevels
    result["confirmed_levels"] = confirmed
    return result
