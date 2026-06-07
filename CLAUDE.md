# Stock Dashboard — Project Guide

## What this is
A personal stock dashboard: Flask backend + SQLite + single-page HTML frontend.
Tracks holdings, sold positions, a monitor/watchlist, breakout signals, a pattern scanner, and a strategy backtester.

## How to start
```
python api_server.py
```
Open browser at http://127.0.0.1:5000
Or double-click `start.bat`.

Optional flags:
- `--port 5001` — change port
- `--user admin --pass secret` — enable Basic Auth (for internet exposure)
- `--debug` — auto-reload on code changes

## Key files
| File | Purpose |
|---|---|
| `api_server.py` | Flask server — all API routes + Basic Auth |
| `db_setup.py` | SQLite schema + migrations (`setup_database()`) |
| `fetch_data.py` | Downloads price data from Yahoo Finance |
| `scan_patterns.py` | All 5 pattern scanners + `scan_date_range()` + `get_scan_results_range()` |
| `backtest_engine.py` | Pure-Python 3-stage exit strategy simulator (no GUI), used by `/api/backtest/*` |
| `scan_cup_handle.py` | Original standalone Cup & Handle scanner (CLI only, not used by dashboard) |
| `templates/dashboard.html` | Entire frontend — HTML + CSS + JS in one file |
| `start.bat` | Double-click launcher |
| `stock_dashboard.db` | SQLite database (do not commit to git) |

## Database tables
| Table | Purpose |
|---|---|
| `stocks_daily` | OHLCV + indicators (MA6/10/30/50/200, High/Low 30D, Vol MA10) per ticker per date |
| `holdings` | Current positions |
| `sold` | Sold positions |
| `monitor_list` | Watchlist |
| `breakout_signals` | Historical signal log |
| `extraction_tickers` | Tickers to fetch from Yahoo Finance |
| `pattern_scan_results` | Stored results from pattern scanner (keyed by scan_date + ticker + pattern_name) |
| `skipped_stocks` | Tickers skipped during fetch |

## API endpoints
```
GET  /                              Dashboard UI
GET  /api/holdings
GET  /api/signals/ma200?from=&to=   Price > MA200 signals
GET  /api/signals/ma1030?from=&to=  MA10 > MA30 signals
GET  /api/sold
GET  /api/monitor
GET  /api/prices
GET  /api/extraction/tickers
POST /api/extraction/tickers        {ticker, notes?}
POST /api/extraction/upload         CSV/XLSX file upload
GET  /api/extraction/download
POST /api/fetch                     {period?} — starts background Yahoo Finance fetch
GET  /api/fetch/status
POST /api/patterns/scan             {date?} or {from_date, to_date} — single or range scan
GET  /api/patterns/scan/status
GET  /api/patterns/results?date=    single date
GET  /api/patterns/results?from=&to= date range
GET  /api/patterns/dates
POST /api/backtest/single           {ticker, start_date, end_date, p0, t1, sl, t2, rev, prot, trail}
POST /api/backtest/batch            auto-loads all holdings (avg_buy_price + buy_date)
POST /api/backtest/selection        {items: [{ticker, p0, start_date}], t1, sl, t2, rev, prot, trail}
```

## Dashboard tabs (in order)
1. **Holdings** — portfolio positions with live P/L + Cut Off price column
2. **Signals** — MA200 and MA10>MA30 breakout signals with date range filter
3. **Sold** — realised P/L history
4. **Monitor** — watchlist with signal price tracking; per-row `+ Backtest` button
5. **Pattern Scanner** — 5 pattern scans, single-date or date-range scan, ticker filter, per-row `+ Backtest` button
6. **Ticker List** — manages extraction_tickers (what gets fetched)
7. **Backtester** — strategy backtester with selection queue

## Holdings — Cut Off Price column
Calculated per row, shown in red:
- **At a loss** (current price ≤ avg buy): `avg_buy_price × 89%`
- **In profit** (current price > avg buy): `high_30d × 89%`

## Pattern Scanner (tab 5)
- 5 patterns defined in `scan_patterns.py` with tuneable CONFIG at top of file
- **Single scan**: POST `/api/patterns/scan` with `{date}` — scans one trading day
- **Range scan**: POST `/api/patterns/scan` with `{from_date, to_date}` — scans every trading day in range; loads full price history once for efficiency
- Results stored in `pattern_scan_results` table (re-scanning same date overwrites)
- Frontend polls `/api/patterns/scan/status` for progress
- Results tab: single-date dropdown OR date range viewer (Load / Clear)
- Ticker filter input: live filters the results table client-side
- Tab state (results, filter, sub-tab) is preserved across tab switches; only fetches on first visit
- `+ Watch` button adds to Monitor; `+ Backtest` button adds to Backtester queue

### Pattern rules (summary)
1. **Cup & Handle** — 60-day cup + 10-day handle + breakout above rim with vol > 1.5× MA10
2. **Golden Cross** — MA10 crosses above MA30 (Signal A) and/or MA50 crosses above MA200 (Signal B)
3. **MA200 Breakout** — close crosses above MA200, vol > 1.2× MA10, MA50 > MA200
4. **Volume Surge** — price up, vol ≥ 1.5× MA10, breaks above High_30D, direction = Up
5. **Pullback Bounce** — close within 5% above Low_30D, above MA200, price bouncing, MA10 > MA30

## Backtester (tab 7)
- **Selection Queue**: stocks added from Monitor or Pattern Scanner via `+ Backtest` button
  - Duplicate check is ticker + start_date (same ticker with different dates can coexist)
  - Queue renders in Backtester tab with editable P0 and start date per row
  - Nav tab badge shows `Backtester (N)` when queue is non-empty
- **Batch — Selection**: runs `/api/backtest/selection` for all queued items
- Strategy rules (all passed as % from frontend, converted to fractions in backend):
  - `t1` — Stage 1 target (sell 50%)
  - `sl` — Stop loss (sell all)
  - `t2` — Stage 2 target (sell half of remaining)
  - `rev` — Reversal threshold (sell remaining)
  - `prot` — Protection level (trailing activation)
  - `trail` — Trailing stop %
- Simulation states: `HOLDING_4 → HOLDING_2 → HOLDING_1 → FULLY_SOLD`
- Price data comes from `stocks_daily` (open, high, low, high_30d) — no file upload needed

## Backtest engine (`backtest_engine.py`)
```python
run_trading_simulation(rows, p_0, start_date, rules)
# rows: list of dicts with keys date, open, high, low, high_30d
# rules: {t1, sl, t2, rev, prot, trail} as fractions (e.g. 0.10)
# returns: list of transaction dicts

calculate_metrics(transactions)
# returns: (initial_cost, total_pnl, roi_pct)
```

## Frontend notes
- All JS/CSS is inline in `templates/dashboard.html` (no build step)
- Tab switching: `showTab(name)` — TABS array (7 items) order must match HTML tab-btn order
- Signal "Add to Monitor" and Pattern "Add to Monitor" both call POST `/api/monitor`
- Pattern rows are colour-coded: blue=Cup&Handle, yellow=GoldenCross, green=MA200, orange=VolSurge, purple=PullbackBounce
- Nav tabs use `overflow-x: auto; scrollbar-width: none` to handle 7 tabs on narrow screens
- `_patternsInitialised` flag prevents re-fetching pattern results on tab revisit

## Basic Auth
Disabled by default (local use). Enable with:
```
python api_server.py --user myname --pass mypassword
```
Or via env vars `DASH_USER` / `DASH_PASS`.
