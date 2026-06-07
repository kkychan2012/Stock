"""
Backtest engine — extracted from Stock_Strategy_Backtester.py (tkinter app).

Core strategy: 3-stage exit for a 4 QTY initial position.
  Stage 1 (HOLDING_4): hit Target 1 → sell 2, or Stop Loss → sell all 4
  Stage 2 (HOLDING_2): hit Target 2 → sell 1, or Reversal → sell remaining 2
  Stage 3 (HOLDING_1): hit Protection → sell 1, or Trailing Stop → sell 1

Both functions are stateless and accept plain Python types — no Flask context needed.
"""

import pandas as pd


def run_trading_simulation(rows, p_0, start_date, rules):
    """
    rows      : list of dicts with keys date, open, high, low, high_30d
    p_0       : float — entry price
    start_date: str   — YYYY-MM-DD, simulation starts on / after this date
    rules     : dict  — {t1, sl, t2, rev, prot, trail} as fractional values (e.g. 0.10)

    Returns a list of transaction dicts.
    """
    if not rows:
        return []

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df = df[df["date"] >= pd.to_datetime(start_date)].reset_index(drop=True)

    if df.empty:
        return []

    status = "HOLDING_4"
    remaining_qty = 4
    last_processed_price = p_0
    last_date_str = pd.to_datetime(start_date).strftime("%Y-%m-%d")

    transactions = [{
        "date": last_date_str, "action": "BUY", "qty": 4, "price": p_0,
        "cash_flow": -4 * p_0, "remaining": 4, "reason": "Initial Position Setup",
    }]

    for _, row in df.iterrows():
        if status == "FULLY_SOLD":
            break

        last_date_str = row["date"].strftime("%Y-%m-%d")
        high = float(row["high"]) if pd.notna(row.get("high")) else 0.0
        low  = float(row["low"])  if pd.notna(row.get("low"))  else 0.0
        last_processed_price = (
            float(row["open"]) if pd.notna(row.get("open")) else high
        )
        high_30d = (
            float(row["high_30d"]) if pd.notna(row.get("high_30d")) else high
        )

        if status == "HOLDING_4":
            if low <= p_0 * (1 - rules["sl"]):
                price = p_0 * (1 - rules["sl"])
                transactions.append({
                    "date": last_date_str, "action": "SELL", "qty": 4,
                    "price": price, "cash_flow": 4 * price,
                    "remaining": 0, "reason": "Stop Loss Hit",
                })
                remaining_qty = 0
                status = "FULLY_SOLD"
            elif high >= p_0 * (1 + rules["t1"]):
                price = p_0 * (1 + rules["t1"])
                transactions.append({
                    "date": last_date_str, "action": "SELL", "qty": 2,
                    "price": price, "cash_flow": 2 * price,
                    "remaining": 2, "reason": "Profit Target 1 Hit",
                })
                remaining_qty = 2
                status = "HOLDING_2"

        elif status == "HOLDING_2":
            if low <= p_0 * (1 + rules["rev"]):
                price = p_0 * (1 + rules["rev"])
                transactions.append({
                    "date": last_date_str, "action": "SELL", "qty": 2,
                    "price": price, "cash_flow": 2 * price,
                    "remaining": 0, "reason": "Reversal Trigger Hit",
                })
                remaining_qty = 0
                status = "FULLY_SOLD"
            elif high >= p_0 * (1 + rules["t2"]):
                price = p_0 * (1 + rules["t2"])
                transactions.append({
                    "date": last_date_str, "action": "SELL", "qty": 1,
                    "price": price, "cash_flow": 1 * price,
                    "remaining": 1, "reason": "Profit Target 2 Hit",
                })
                remaining_qty = 1
                status = "HOLDING_1"

        elif status == "HOLDING_1":
            if low <= p_0 * (1 + rules["prot"]):
                price = p_0 * (1 + rules["prot"])
                transactions.append({
                    "date": last_date_str, "action": "SELL", "qty": 1,
                    "price": price, "cash_flow": 1 * price,
                    "remaining": 0, "reason": "Protection Threshold Hit",
                })
                remaining_qty = 0
                status = "FULLY_SOLD"
            elif low <= high_30d * (1 - rules["trail"]):
                price = high_30d * (1 - rules["trail"])
                transactions.append({
                    "date": last_date_str, "action": "SELL", "qty": 1,
                    "price": price, "cash_flow": 1 * price,
                    "remaining": 0, "reason": "Trailing Stop Hit",
                })
                remaining_qty = 0
                status = "FULLY_SOLD"

    if remaining_qty > 0:
        transactions.append({
            "date": last_date_str, "action": "OPEN HOLDING",
            "qty": remaining_qty, "price": last_processed_price,
            "cash_flow": remaining_qty * last_processed_price,
            "remaining": remaining_qty, "reason": "Horizon Cap (Asset Open)",
        })

    return transactions


def calculate_metrics(txs):
    """Returns (initial_cost, total_pnl, roi_pct) from a transactions list."""
    initial_cost  = sum(abs(t["cash_flow"]) for t in txs if t["action"] == "BUY")
    total_returns = sum(t["cash_flow"] for t in txs if t["action"] != "BUY")
    total_pnl     = total_returns - initial_cost
    roi           = (total_pnl / initial_cost * 100) if initial_cost > 0 else 0.0
    return initial_cost, total_pnl, roi
