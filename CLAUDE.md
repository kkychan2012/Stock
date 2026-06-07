# Stock Dashboard — Project Guide

## What this is
A personal stock dashboard: Flask backend + SQLite + single-page HTML frontend.
Tracks holdings, sold positions, a monitor/watchlist, breakout signals, and a pattern scanner.

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
| `scan_patterns.py` | All 5 pattern scanners (Cup & Handle, Golden Cross, MA200 Breakout, Volume Surge, Pullback Bounce) |
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
POST /api/patterns/scan             {date?} — starts background pattern scan
GET  /api/patterns/scan/status
GET  /api/patterns/results?date=
GET  /api/patterns/dates
```

## Dashboard tabs (in order)
1. **Holdings** — portfolio positions with live P/L
2. **Signals** — MA200 and MA10>MA30 breakout signals with date range filter
3. **Sold** — realised P/L history
4. **Monitor** — watchlist with signal price tracking
5. **Pattern Scanner** — 5 pattern scans stored in DB, sub-tabs per pattern, Add to Monitor button
6. **Ticker List** — manages extraction_tickers (what gets fetched)

## Pattern Scanner (tab 5)
- 5 patterns defined in `scan_patterns.py` with tuneable CONFIG at top of file
- Scan is triggered via POST /api/patterns/scan, runs in background thread
- Results stored in `pattern_scan_results` table (re-scanning same date overwrites)
- Frontend polls /api/patterns/scan/status for progress
- "Add to Monitor" button works same as Signals page

### Pattern rules (summary)
1. **Cup & Handle** — 60-day cup + 10-day handle + breakout above rim with vol > 1.5× MA10
2. **Golden Cross** — MA10 crosses above MA30 (Signal A) and/or MA50 crosses above MA200 (Signal B)
3. **MA200 Breakout** — close crosses above MA200, vol > 1.2× MA10, MA50 > MA200
4. **Volume Surge** — price up, vol ≥ 1.5× MA10, breaks above High_30D, direction = Up
5. **Pullback Bounce** — close within 5% above Low_30D, above MA200, price bouncing, MA10 > MA30

## Frontend notes
- All JS/CSS is inline in `templates/dashboard.html` (no build step)
- Tab switching: `showTab(name)` — TABS array order must match HTML tab-btn order
- Signal "Add to Monitor" and Pattern "Add to Monitor" both call POST /api/monitor
- Pattern rows are colour-coded: blue=Cup&Handle, yellow=GoldenCross, green=MA200, orange=VolSurge, purple=PullbackBounce
- Nav tabs use `overflow-x: auto; scrollbar-width: none` to handle 6 tabs on narrow screens

## Basic Auth
Disabled by default (local use). Enable with:
```
python api_server.py --user myname --pass mypassword
```
Or via env vars `DASH_USER` / `DASH_PASS`.
