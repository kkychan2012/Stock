from scan_cup_handle import _load_prices, _scan_ticker, _deduplicate, CUP_MIN_DAYS, HANDLE_MIN
from db_setup import get_connection

all_prices = _load_prices()
results = []
for ticker, records in all_prices.items():
    if len(records) < CUP_MIN_DAYS + HANDLE_MIN + 5:
        continue
    found = _scan_ticker(ticker, records)
    if found:
        results.extend(found)

results = _deduplicate(results)

# One best (deepest) pattern per ticker
best_per_ticker = {}
for r in results:
    t = r["ticker"]
    if t not in best_per_ticker or r["depth_pct"] > best_per_ticker[t]["depth_pct"]:
        best_per_ticker[t] = r

final = sorted(best_per_ticker.values(), key=lambda x: x["breakout_date"], reverse=True)

print(f"Best pattern per ticker: {len(final)} stocks\n")
header = f"{'Ticker':<7} {'Cup Start':<12} {'Bottom':<12} {'Cup End':<12} {'Breakout':<12} {'Depth':>7}  {'Cup Days':>8}  {'vs Rim':>8}  {'Left Rim':>9}  {'Low':>9}"
print(header)
print("-" * len(header))
for r in final[:20]:
    gain = (r["breakout_price"] - r["right_rim"]) / r["right_rim"] * 100
    print(f"{r['ticker']:<7} {r['cup_start']:<12} {r['cup_bottom_date']:<12} {r['cup_end']:<12} "
          f"{r['breakout_date']:<12} {r['depth_pct']:>6.1f}%  {r['cup_days']:>8}  {gain:>+7.1f}%  "
          f"{r['left_rim']:>9.2f}  {r['bottom']:>9.2f}")

# ── Verify one clean example with actual price rows ──
print("\n\n=== Verified example: GE ===")
ex = best_per_ticker.get("GE")
if not ex:
    ex = final[0]

print(f"Ticker      : {ex['ticker']}")
print(f"Cup start   : {ex['cup_start']}  @ ${ex['left_rim']:.2f}")
print(f"Cup bottom  : {ex['cup_bottom_date']}  @ ${ex['bottom']:.2f}  ({ex['depth_pct']:.1f}% drop)")
print(f"Cup end     : {ex['cup_end']}  @ ${ex['right_rim']:.2f}")
print(f"Breakout    : {ex['breakout_date']}  @ ${ex['breakout_price']:.2f}")
print(f"Cup length  : {ex['cup_days']} trading days")

with get_connection() as conn:
    rows = conn.execute(
        "SELECT date, close FROM stocks_daily WHERE ticker=? AND date BETWEEN ? AND ? ORDER BY date",
        (ex["ticker"], ex["cup_start"], ex["breakout_date"])
    ).fetchall()

print(f"\nKey price points across the pattern:")
milestones = {ex["cup_start"], ex["cup_bottom_date"], ex["cup_end"], ex["breakout_date"]}
for row in rows:
    if row["date"] in milestones:
        label = ""
        if row["date"] == ex["cup_start"]:       label = " ← Left rim"
        elif row["date"] == ex["cup_bottom_date"]:label = " ← Cup bottom"
        elif row["date"] == ex["cup_end"]:        label = " ← Right rim / handle start"
        elif row["date"] == ex["breakout_date"]:  label = " ← BREAKOUT"
        print(f"  {row['date']}  ${row['close']:.2f}{label}")
