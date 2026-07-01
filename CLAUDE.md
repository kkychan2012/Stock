# Stock Dashboard — Project Guide

## What this is
A personal stock dashboard: Flask backend + SQLite + single-page HTML frontend.
Tracks holdings, sold positions, named watchlists, breakout/RSI signals, a 7-pattern scanner,
insider trading signals, IBD-style market health (Distribution/Follow-Through Days),
and a strategy backtester.

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
| `fetch_data.py` | Downloads price data from Yahoo Finance; computes indicators, RS Rank, IBD Trend Template criteria |
| `scan_patterns.py` | All 7 pattern scanners + `scan_date_range()` + `get_scan_results_range()` |
| `backtest_engine.py` | Pure-Python 3-stage exit strategy simulator (no GUI), used by `/api/backtest/*` |
| `market_health.py` | IBD-style Distribution Day / Follow-Through Day market health tracker (Nasdaq + S&P 500) |
| `Insider Trading/insider_pipeline/` | Standalone pipeline (SEC Form 4/6-K, Nasdaq Nordic, FI Insynsregistret) invoked by `/api/insider/scan`; writes into `insider_signals` |
| `scan_cup_handle.py` | Original standalone Cup & Handle scanner (CLI only, not used by dashboard) |
| `templates/dashboard.html` | Entire frontend — HTML + CSS + JS in one file |
| `start.bat` | Double-click launcher |
| `stock_dashboard.db` | SQLite database (do not commit to git) |

## Database tables
| Table | Purpose |
|---|---|
| `stocks_daily` | OHLCV + indicators (MA6/10/30/50/150/200, High/Low 30D & 52wk, Vol MA10, RS Rank, IBD Trend Template criteria c1–c8 + trend_score) per ticker per date |
| `holdings` | Current positions |
| `sold` | Sold positions |
| `watchlists` | Named watchlists (id, name) — `id=1` is the default list |
| `monitor_list` | Watchlist entries, each tied to a `watchlist_id` |
| `breakout_signals` | Historical signal log |
| `extraction_tickers` | Tickers to fetch from Yahoo Finance |
| `pattern_scan_results` | Stored results from pattern scanner (keyed by scan_date + ticker + pattern_name) |
| `skipped_stocks` | Tickers skipped during fetch |
| `insider_signals` | Insider Form 4/6-K buy/sell signals (cluster-buy + Rule 10b5-1 flags) |

## API endpoints
```
GET  /                                  Dashboard UI

# Holdings
GET    /api/holdings
POST   /api/holdings
POST   /api/holdings/<id>/sell
DELETE /api/holdings/<id>
POST   /api/holdings/upload             CSV/XLSX bulk upload
GET    /api/holdings/download

# Sold
GET    /api/sold
POST   /api/sold
DELETE /api/sold/<id>
POST   /api/sold/upload
GET    /api/sold/download

# Signals
GET  /api/signals/ma200                 Price > MA200
GET  /api/signals/ma1030                MA10 > MA30
GET  /api/signals/ma200bear             Price < MA200 (bearish)
GET  /api/signals/ma1030bear            MA10 < MA30 (bearish)
GET  /api/signals/all
GET  /api/signals/rsi                   RSI-based signals

# Watchlists (named lists) + Monitor
GET    /api/watchlists
POST   /api/watchlists
DELETE /api/watchlists/<id>
GET    /api/monitor?watchlist=<id>
GET    /api/monitor/tickers?watchlist=<id>
POST   /api/monitor                     {ticker, watchlist_id?, ...}
DELETE /api/monitor/<id>
POST   /api/monitor/upload
GET    /api/monitor/download

# Insider trading
GET    /api/insider?from=&to=
DELETE /api/insider/<id>
POST   /api/insider/scan                {tickers, from_date, to_date?} — runs insider_pipeline
GET    /api/insider/scan/status

# Market health (Distribution Day / Follow-Through Day)
GET  /api/market/status?refresh=1       cached per calendar day unless refresh=1

# Prices / ticker list
GET    /api/prices
GET    /api/extraction/tickers
POST   /api/extraction/tickers          {ticker, notes?}
DELETE /api/extraction/tickers/<ticker>
DELETE /api/extraction/tickers
POST   /api/extraction/upload           CSV/XLSX file upload
GET    /api/extraction/download

# Fetch
POST /api/fetch                         {period?} — starts background Yahoo Finance fetch
GET  /api/fetch/status

# Pattern scanner
POST /api/patterns/scan                 {date?} or {from_date, to_date} — single or range scan
GET  /api/patterns/scan/status
GET  /api/patterns/results?date=        single date
GET  /api/patterns/results?from=&to=    date range
GET  /api/patterns/dates

# Backtester
POST /api/backtest/single               {ticker, start_date, end_date, p0, t1, sl, t2, rev, prot, trail}
POST /api/backtest/batch                auto-loads all holdings (avg_buy_price + buy_date)
POST /api/backtest/selection            {items: [{ticker, p0, start_date}], t1, sl, t2, rev, prot, trail}

# Backup / restore
GET  /api/backup/download               download stock_dashboard.db
POST /api/backup/restore                upload a .db file to replace it (atomic swap)

# Data browser (tab 10, diagnostics)
GET /api/data/summary                   latest snapshot per ticker (all indicator columns)
GET /api/data/history?ticker=&limit=     full OHLCV+indicator history for one ticker
GET /api/debug/trend-template?ticker=    last 5 rows with Trend Template criteria breakdown
```

## Dashboard tabs (in order)
1. **Holdings** — portfolio positions with live P/L + Cut Off price column
2. **Signals** — MA200 and MA10>MA30 breakout signals (bull + bear) with date range filter
3. **RSI Signals** — RSI-based breakout signals
4. **Sold** — realised P/L history
5. **Monitor** — named watchlists with signal price tracking; per-row `+ Backtest` button
6. **Pattern Scanner** — 7 pattern scans, single-date or date-range scan, ticker filter, per-row `+ Backtest` button
7. **Ticker List** — manages extraction_tickers (what gets fetched)
8. **Backtester** — strategy backtester with selection queue
9. **Insider** — insider Form 4/6-K buy/sell signals, cluster-buy detection, on-demand scan by ticker + date range
10. **Market** — IBD-style Distribution Day / Follow-Through Day market health (Nasdaq + S&P 500), status banner + 60-day chart
11. **Data** — raw data browser / diagnostics for `stocks_daily` (latest snapshot, per-ticker history, Trend Template criteria)

`TABS` array in `dashboard.html` (`['holdings','signals','rsi','sold','monitor','patterns','tickers','backtester','insider','market','dataview']`) order must match the HTML tab-btn order.

## Holdings — Cut Off Price column
Calculated per row, shown in red:
- **At a loss** (current price ≤ avg buy): `avg_buy_price × 89%`
- **In profit** (current price > avg buy): `high_30d × 89%`

## Named Watchlists (Monitor tab)
- `watchlists` table holds named lists; `monitor_list.watchlist_id` ties each entry to one (default `id=1`).
- `GET/POST/DELETE /api/watchlists` manage the lists themselves; `/api/monitor?watchlist=<id>` filters entries.
- Deleting a watchlist cascades and deletes its `monitor_list` entries.

## Pattern Scanner (tab 6)
- 7 patterns defined in `scan_patterns.py` with tuneable CONFIG at top of file
- **Single scan**: POST `/api/patterns/scan` with `{date}` — scans one trading day
- **Range scan**: POST `/api/patterns/scan` with `{from_date, to_date}` — scans every trading day in range; loads full price history once for efficiency
- Results stored in `pattern_scan_results` table (re-scanning same date overwrites)
- Frontend polls `/api/patterns/scan/status` for progress
- Results tab: single-date dropdown OR date range viewer (Load / Clear)
- Ticker filter input: live filters the results table client-side
- Tab state (results, filter, sub-tab) is preserved across tab switches; only fetches on first visit
- `+ Watch` button adds to Monitor (asks which named watchlist); `+ Backtest` button adds to Backtester queue

### Pattern rules (summary)
1. **Cup & Handle** — 60-day cup + 10-day handle + breakout above rim with vol > 1.5× MA10
2. **Golden Cross** — MA10 crosses above MA30 (Signal A) and/or MA50 crosses above MA200 (Signal B)
3. **MA200 Breakout** — close crosses above MA200, vol > 1.2× MA10, MA50 > MA200
4. **Volume Surge** — price up, vol ≥ 1.5× MA10, breaks above High_30D, direction = Up
5. **Pullback Bounce** — close within 5% above Low_30D, above MA200, price bouncing, MA10 > MA30
6. **Momentum Expansion** — ≥12 of last 15 days up, close gained ≥20% over the 15-day window, recent 15D avg volume ≥1.25× the prior 15D window
7. **Momentum 10/8** — ≥8 of last 10 days up, recent 10D avg volume ≥1.25× the prior 10D window

## Insider tab
- Backed by `insider_signals`, populated by running `Insider Trading/insider_pipeline/main.py` as a subprocess via `POST /api/insider/scan {tickers, from_date, to_date}`.
- Sources: SEC Form 4, Form 6-K, Nasdaq Nordic filings, FI Insynsregistret (Sweden) — captures both buys and sells.
- Flags cluster buys (`cluster_buy`) and Rule 10b5-1 pre-scheduled sales (`flag_10b51`).
- `GET /api/insider/scan/status` polls background scan progress/log; scans are serialized (409 if already running).

## Market tab (Distribution Day / Follow-Through Day)
- `market_health.py` fetches ^IXIC (Nasdaq) and ^GSPC (S&P 500) via yfinance and classifies market health IBD-style.
- **Distribution Day**: close down ≥0.2% on higher volume than prior day; expires after 25 trading days or a 5%+ recovery above that day's close.
- **Follow-Through Day**: on rally day 4–20 (from most recent low), close up ≥1.25% on higher volume.
- **Status** (higher of the two indices' active DD counts): 0–3 Healthy, 4–5 Caution, 6+ Correction Likely, 6+ with a recent FTD → New Uptrend Confirmed (override).
- Result cached in memory per calendar day; `GET /api/market/status?refresh=1` forces a re-fetch.
- Tuneable thresholds live at the top of `market_health.py`.

## RS Rank / IBD Trend Template (fetch pipeline)
- `fetch_data.py` computes MA150, 52-week high/low, and a raw Relative Strength score during fetch, then ranks tickers into `rs_rank` (percentile).
- Also evaluates IBD's 8-criteria Trend Template (columns `c1`–`c8`, aggregate `trend_score`) per ticker per day, e.g. Close > MA150 & MA200, MA150 > MA200, MA200 trending up, Close > MA50, RS Rank ≥ 70, within 25% of 52-week high, etc.
- Inspect via `GET /api/debug/trend-template?ticker=` (last 5 rows + criteria breakdown) or the Data tab.

## Backtester (tab 8)
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

## Backup / Restore
- `GET /api/backup/download` streams `stock_dashboard.db` as a file attachment.
- `POST /api/backup/restore` accepts a multipart `file` upload, validates the SQLite header magic bytes, then atomically replaces the live DB (write to temp file, `os.replace`).

## Frontend notes
- All JS/CSS is inline in `templates/dashboard.html` (no build step)
- Tab switching: `showTab(name)` — `TABS` array (11 items) order must match HTML tab-btn order
- Signal "Add to Monitor" and Pattern "Add to Monitor" both call POST `/api/monitor`
- Pattern rows are colour-coded per pattern type
- Nav tabs use `overflow-x: auto; scrollbar-width: none` to handle many tabs on narrow screens
- `_patternsInitialised` flag prevents re-fetching pattern results on tab revisit

## Basic Auth
Disabled by default (local use). Enable with:
```
python api_server.py --user myname --pass mypassword
```
Or via env vars `DASH_USER` / `DASH_PASS`.

## Not part of the dashboard
- `Insider Trading/insider_pipeline/` is invoked by the dashboard (see above) but can also run standalone via its own `main.py` / `gui.py`.
- `scan_cup_handle.py`, `Analysis_By_Volumn.py`, `Interrogate_Stock_file3.py`, `Stock_Figure_Extract_GUI.py`, `Stock_Strategy_Backtester.py`, and everything under `Source Code backup/` are legacy/standalone scripts predating the dashboard — not imported by `api_server.py`.
- The `Football Video/` directory is an unrelated project that happens to live under this path; ignore it for dashboard work.
