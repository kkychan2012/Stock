"""
Capital-recycling portfolio simulation for the volume-surge Scenario A study
(Jan-Aug 2026, 15% partial target, MA21 cut-loss). Starts with a fixed pool
of cash, deploys a fixed position size per trade, and reuses freed-up cash
(from partial sells and final exits) to fund later signals -- skipping a
signal if no cash is free at the moment it fires.

Writes a full transaction log to an .xlsx file (Transactions, Skipped
Signals, Summary sheets) for manual review in Excel.

Standalone: reads only the already-generated
strategies/study_volume_surge_{YYYY}_{MM}.json files (2025 full year +
2026 Jan-Aug). Does not touch strategy_log.json or any other project file.
"""
import json
import sys
import pandas as pd

def _find_months(data_dir="strategies"):
    """Every monthly study file present (study_volume_surge_YYYY_MM.json), oldest first.
    Run from the folder that contains ./strategies/ (project root for the US study,
    hk/ for the Hong Kong one)."""
    import glob
    import os
    import re
    months = []
    for p in glob.glob(os.path.join(data_dir, "study_volume_surge_*.json")):
        m = re.search(r"study_volume_surge_(\d{4}_\d{2})\.json$", p)
        if m:
            months.append(m.group(1))
    return sorted(months)


def run_simulation(starting_capital, position_size, out_path, skip_if_held=False, compound_slots=None,
                   max_pos_pct=None):
    """compound_slots=N: profits compound. Each buy is sized at
    cash_on_hand / (N - positions_held), so realized profit is spread across
    the still-free slots rather than dumped into the next trade. position_size
    is ignored in this mode unless > 0, in which case it is the base stake and
    buys are sized base + (profit above base) / free slots, floored at base."""
    all_data = [json.load(open(f'strategies/study_volume_surge_{m}.json')) for m in _find_months()]
    trades = [t for d in all_data for t in d['trades'] if t['scenario'] == 'A']

    events = []
    for t in trades:
        events.append((t['entry_date'], 0, 'BUY', t))
        if t['partial_sell']:
            events.append((t['partial_sell']['date'], -1, 'SELL_HALF', t))
        if t['status'] == 'closed':
            events.append((t['exit_date'], -1, 'SELL_FINAL', t))

    events.sort(key=lambda e: (e[0], e[1], e[3]['ticker'], e[3]['surge_date']))

    def tid(t):
        return (t['ticker'], t['surge_date'], t['scenario'])

    cash = starting_capital
    active = {}
    held_tickers = set()
    log_rows = []
    skipped_rows = []
    executed_keys = []
    sizes = {}  # trade key -> dollars invested at entry

    for date, _prio, kind, t in events:
        key = tid(t)
        if kind == 'BUY':
            if compound_slots:
                free = compound_slots - len(active)
                if free <= 0:
                    size = 0.0
                elif position_size > 0:
                    # base + distributed profit: $position_size plus the cash above one base
                    # stake per free slot, split across the free slots. Never below the base
                    # (losses don't shrink the stake). Same as cash/free when cash >= base*free.
                    size = position_size + max(0.0, cash - position_size * free) / free
                else:
                    size = cash / free
            else:
                size = position_size
            if max_pos_pct:
                # equity at cost: cash + cost basis still held (a half-sold position keeps half)
                equity = cash + sum(p['invested'] * (0.5 if p['half_sold'] else 1.0) for p in active.values())
                size = min(size, max_pos_pct * equity)
            if skip_if_held and t['ticker'] in held_tickers:
                skipped_rows.append({
                    'date': date, 'ticker': t['ticker'], 'surge_date': t['surge_date'],
                    'entry_price': t['entry_price'], 'reason': 'ticker already held (skip-if-held enabled)',
                })
            elif size > 0 and cash >= size - 1e-9:
                cash -= size
                sizes[key] = size
                active[key] = {'invested': size, 'entry_price': t['entry_price'], 'half_sold': False}
                held_tickers.add(t['ticker'])
                executed_keys.append(key)
                log_rows.append({
                    'date': date, 'ticker': t['ticker'], 'surge_date': t['surge_date'],
                    'action': 'BUY', 'price': t['entry_price'], 'shares': round(size / t['entry_price'], 4),
                    'cash_flow': -round(size, 2), 'cash_balance_after': round(cash, 2),
                    'note': f"entry (Scenario A, surge close {t['surge_close']})",
                })
            else:
                skipped_rows.append({
                    'date': date, 'ticker': t['ticker'], 'surge_date': t['surge_date'],
                    'entry_price': t['entry_price'],
                    'reason': ('all slots full' if compound_slots and size <= 0
                               else f'insufficient cash (had ${cash:,.2f}, needed ${size:,.2f})'),
                })
        elif kind == 'SELL_HALF':
            if key in active and not active[key]['half_sold']:
                pos = active[key]
                price = t['partial_sell']['price']
                half_value = pos['invested'] * 0.5 * (price / pos['entry_price'])
                cash += half_value
                pos['half_sold'] = True
                log_rows.append({
                    'date': date, 'ticker': t['ticker'], 'surge_date': t['surge_date'],
                    'action': 'SELL_HALF', 'price': price, 'shares': round((pos['invested'] / pos['entry_price']) * 0.5, 4),
                    'cash_flow': round(half_value, 2), 'cash_balance_after': round(cash, 2),
                    'note': '28%/24%/15% partial-sell target hit',
                })
        elif kind == 'SELL_FINAL':
            if key in active:
                pos = active[key]
                price = t['exit_price']
                fraction = 0.5 if pos['half_sold'] else 1.0
                final_value = pos['invested'] * fraction * (price / pos['entry_price'])
                cash += final_value
                log_rows.append({
                    'date': date, 'ticker': t['ticker'], 'surge_date': t['surge_date'],
                    'action': 'SELL_FINAL' if not pos['half_sold'] else 'SELL_REMAINING_HALF',
                    'price': price, 'shares': round((pos['invested'] / pos['entry_price']) * fraction, 4),
                    'cash_flow': round(final_value, 2), 'cash_balance_after': round(cash, 2),
                    'note': t['exit_type'],
                })
                del active[key]
                held_tickers.discard(t['ticker'])

    open_rows = []
    open_value = 0.0
    for key, pos in active.items():
        t = next(tr for tr in trades if tid(tr) == key)
        fraction = 0.5 if pos['half_sold'] else 1.0
        mark_price = t['last_close'] if t['status'] == 'still_open' else t['exit_price']
        mv = pos['invested'] * fraction * (mark_price / pos['entry_price'])
        open_value += mv
        open_rows.append({
            'ticker': t['ticker'], 'surge_date': t['surge_date'], 'entry_date': t['entry_date'],
            'entry_price': pos['entry_price'], 'shares': round((pos['invested'] / pos['entry_price']) * fraction, 4),
            'last_close': mark_price, 'unrealized_value': round(mv, 2),
            'invested_fraction': 'remaining half' if pos['half_sold'] else 'full position',
        })

    ending_total = cash + open_value

    # One row per trade: buy leg + partial-sell leg + final-sell leg, with P/L,
    # for reviewing trades individually rather than as a flat event stream.
    trade_rows = []
    for key in executed_keys:
        t = next(tr for tr in trades if tid(tr) == key)
        entry_price = t['entry_price']
        invested = sizes[key]
        shares_at_entry = invested / entry_price

        if t['partial_sell']:
            partial_price = t['partial_sell']['price']
            partial_shares = shares_at_entry * 0.5
            partial_proceeds = partial_shares * partial_price
            final_fraction = 0.5
        else:
            partial_price = partial_shares = partial_proceeds = None
            final_fraction = 1.0

        final_shares = shares_at_entry * final_fraction
        if t['status'] == 'closed':
            final_price = t['exit_price']
            final_date = t['exit_date']
            status = 'closed'
        else:
            final_price = t['last_close']
            final_date = t['last_date']
            status = 'still open (unrealized)'
        final_proceeds = final_shares * final_price

        total_proceeds = (partial_proceeds or 0) + final_proceeds
        total_pnl = total_proceeds - invested
        total_pnl_pct = 100 * total_pnl / invested

        trade_rows.append({
            'ticker': t['ticker'],
            'scenario': t['scenario'],
            'surge_date': t['surge_date'],
            'drop_date': t['drop_date'],
            'buy_date': t['entry_date'],
            'buy_price': entry_price,
            'shares_bought': round(shares_at_entry, 4),
            'invested': round(invested, 2),
            'partial_sell_date': t['partial_sell']['date'] if t['partial_sell'] else None,
            'partial_sell_price': partial_price,
            'partial_sell_shares': round(partial_shares, 4) if partial_shares else None,
            'partial_sell_proceeds': round(partial_proceeds, 2) if partial_proceeds else None,
            'final_sell_date': final_date,
            'final_sell_price': round(final_price, 4),
            'final_sell_shares': round(final_shares, 4),
            'final_sell_proceeds': round(final_proceeds, 2),
            'exit_type': t.get('exit_type'),
            'status': status,
            'total_proceeds': round(total_proceeds, 2),
            'total_pnl': round(total_pnl, 2),
            'total_pnl_pct': round(total_pnl_pct, 2),
        })

    df_trades = pd.DataFrame(trade_rows).sort_values('buy_date').reset_index(drop=True)

    df_log = pd.DataFrame(log_rows)
    df_skipped = pd.DataFrame(skipped_rows)
    df_open = pd.DataFrame(open_rows)
    df_summary = pd.DataFrame([{
        'starting_capital': starting_capital,
        'position_size_per_trade': (f'dynamic: cash / ({compound_slots} - held)' if compound_slots else position_size),
        'max_concurrent_positions': compound_slots or int(starting_capital // position_size),
        'total_signals': len(trades),
        'trades_executed': len({r['ticker'] + r['surge_date'] for r in log_rows if r['action'] == 'BUY'}),
        'trades_skipped': len(skipped_rows),
        'ending_cash': round(cash, 2),
        'value_in_open_positions': round(open_value, 2),
        'total_ending_value': round(ending_total, 2),
        'total_pnl': round(ending_total - starting_capital, 2),
        'total_return_pct': round(100 * (ending_total - starting_capital) / starting_capital, 2),
    }])

    with pd.ExcelWriter(out_path, engine='openpyxl') as writer:
        df_summary.to_excel(writer, sheet_name='Summary', index=False)
        df_trades.to_excel(writer, sheet_name='Trades (grouped, with PL)', index=False)
        df_log.to_excel(writer, sheet_name='Transactions (chronological)', index=False)
        df_open.to_excel(writer, sheet_name='Still Open Positions', index=False)
        df_skipped.to_excel(writer, sheet_name='Skipped Signals', index=False)

    print(f"Starting capital: ${starting_capital:,.2f}  |  Position size: ${position_size:,.2f}")
    print(f"Executed: {df_summary.iloc[0]['trades_executed']}  Skipped: {len(skipped_rows)}")
    print(f"Ending value: ${ending_total:,.2f}  (P/L ${ending_total-starting_capital:,.2f}, {100*(ending_total-starting_capital)/starting_capital:.2f}%)")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    starting = float(sys.argv[1]) if len(sys.argv) > 1 else 10000.0
    pos_size = float(sys.argv[2]) if len(sys.argv) > 2 else 1000.0
    skip_held = len(sys.argv) > 3 and sys.argv[3].lower() in ("skip_if_held", "true", "1")
    out = sys.argv[4] if len(sys.argv) > 4 else f"strategies/capital_simulation_{int(starting)}.xlsx"
    compound = int(sys.argv[5]) if len(sys.argv) > 5 else None
    cap_pct = float(sys.argv[6]) if len(sys.argv) > 6 else None   # e.g. 0.10 = max 10% of equity per position
    run_simulation(starting, pos_size, out, skip_if_held=skip_held, compound_slots=compound, max_pos_pct=cap_pct)
