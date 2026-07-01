"""
Cup & Handle pattern scanner against stocks_daily.

Cup:    U-shaped decline and recovery
        - Left rim to right rim: both near the same price (within 5%)
        - Depth: 12-40% drop from rim to bottom
        - Duration: 30-150 trading days
        - Rounded bottom: lowest point falls in the middle 25-75% of the cup

Handle: Brief consolidation after right rim
        - Duration: 5-25 trading days
        - Pullback: less than 12% below right rim
        - Drifts down or sideways (does not make new cup lows)

Breakout: Close above the right rim after the handle
"""

import pandas as pd
import numpy as np
from db_setup import get_connection


# ── Tuneable parameters ───────────────────────────────────────────────────────
CUP_MIN_DAYS   = 30
CUP_MAX_DAYS   = 150
CUP_MIN_DEPTH  = 0.12   # 12% minimum drop
CUP_MAX_DEPTH  = 0.40   # 40% maximum drop
RIM_TOLERANCE  = 0.05   # right rim within 5% of left rim
HANDLE_MIN     = 5
HANDLE_MAX     = 25
HANDLE_MAX_DROP= 0.12   # handle pullback < 12%
BREAKOUT_MIN   = 0.005  # close at least 0.5% above rim


def _load_prices():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT ticker, date, close, volume FROM stocks_daily "
            "WHERE close IS NOT NULL ORDER BY ticker, date"
        ).fetchall()
    data = {}
    for r in rows:
        data.setdefault(r["ticker"], []).append((r["date"], r["close"], r["volume"]))
    return data


def _scan_ticker(ticker, records):
    closes  = np.array([r[1] for r in records])
    dates   = [r[0] for r in records]
    n       = len(closes)
    found   = []

    # right rim index: start from CUP_MIN_DAYS + HANDLE_MIN in
    for r_idx in range(CUP_MIN_DAYS, n - HANDLE_MIN - 1):
        right_rim = closes[r_idx]

        for cup_len in range(CUP_MIN_DAYS, min(CUP_MAX_DAYS, r_idx) + 1):
            l_idx     = r_idx - cup_len
            left_rim  = closes[l_idx]

            # Both rims must be close in price
            if abs(right_rim - left_rim) / left_rim > RIM_TOLERANCE:
                continue

            cup_seg   = closes[l_idx : r_idx + 1]
            bot_local = int(np.argmin(cup_seg))
            bot_idx   = l_idx + bot_local
            bottom    = closes[bot_idx]

            # Depth check
            depth = (left_rim - bottom) / left_rim
            if not (CUP_MIN_DEPTH <= depth <= CUP_MAX_DEPTH):
                continue

            # Rounded bottom: lowest point in middle 25-75% of cup
            bot_pos = bot_local / cup_len
            if not (0.25 <= bot_pos <= 0.75):
                continue

            # Handle: next HANDLE_MIN..HANDLE_MAX bars
            handle_end = min(r_idx + HANDLE_MAX, n - 1)
            if handle_end <= r_idx + HANDLE_MIN:
                continue
            handle_seg = closes[r_idx : handle_end + 1]
            if np.min(handle_seg) < bottom:          # handle can't make new lows
                continue
            handle_drop = (right_rim - np.min(handle_seg)) / right_rim
            if handle_drop > HANDLE_MAX_DROP:
                continue

            # Breakout: first close above right rim after handle
            for b_idx in range(r_idx + HANDLE_MIN, handle_end + 1):
                if closes[b_idx] >= right_rim * (1 + BREAKOUT_MIN):
                    found.append({
                        "ticker":         ticker,
                        "cup_start":      dates[l_idx],
                        "cup_bottom_date":dates[bot_idx],
                        "cup_end":        dates[r_idx],
                        "breakout_date":  dates[b_idx],
                        "left_rim":       round(left_rim, 2),
                        "bottom":         round(bottom, 2),
                        "right_rim":      round(right_rim, 2),
                        "breakout_price": round(closes[b_idx], 2),
                        "depth_pct":      round(depth * 100, 1),
                        "cup_days":       cup_len,
                    })
                    break

            if found and found[-1]["cup_end"] == dates[r_idx]:
                break   # one pattern per right-rim point is enough

    return found


def _deduplicate(results):
    """Keep one pattern per (ticker, cup_start): the one with the deepest cup."""
    best = {}
    for r in results:
        key = (r["ticker"], r["cup_start"])
        if key not in best or r["depth_pct"] > best[key]["depth_pct"]:
            best[key] = r
    return list(best.values())


def scan_all(max_results=15):
    print("Loading price data from DB …")
    all_prices = _load_prices()
    print(f"Scanning {len(all_prices)} tickers …\n")

    results = []
    for ticker, records in all_prices.items():
        if len(records) < CUP_MIN_DAYS + HANDLE_MIN + 5:
            continue
        results.extend(_scan_ticker(ticker, records))

    if not results:
        print("No cup-and-handle patterns found in the database.")
        return

    results = _deduplicate(results)
    # Sort by most recent breakout first
    results.sort(key=lambda x: x["breakout_date"], reverse=True)

    print(f"Found {len(results)} unique pattern(s). Showing top {min(max_results, len(results))}:\n")
    print(f"{'Ticker':<8} {'Cup Start':<12} {'Bottom Date':<12} {'Cup End':<12} "
          f"{'Breakout':<12} {'Left Rim':>9} {'Bottom':>9} {'Depth':>7} "
          f"{'Cup Days':>9} {'Breakout $':>11}")
    print("-" * 105)
    for r in results[:max_results]:
        print(f"{r['ticker']:<8} {r['cup_start']:<12} {r['cup_bottom_date']:<12} "
              f"{r['cup_end']:<12} {r['breakout_date']:<12} "
              f"{r['left_rim']:>9.2f} {r['bottom']:>9.2f} "
              f"{r['depth_pct']:>6.1f}% {r['cup_days']:>9} "
              f"{r['breakout_price']:>11.2f}")

    return results


if __name__ == "__main__":
    scan_all()
