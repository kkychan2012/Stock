# Stock Dashboard — Project Guide

## What this is
A personal stock dashboard: Flask backend + SQLite + single-page HTML frontend.
Tracks holdings, sold positions, named watchlists, breakout/RSI signals, a 9-pattern scanner
(incl. VCP), insider trading signals, Congress/political stock trade disclosures (House + Senate),
IBD-style market health (Distribution/Follow-Through Days), an IBD-style RS Rating Screener merged
into the Data tab (S&P 500 + Russell 1000 universe), a per-ticker Volume Profile / pivot-based
support-resistance module, and a strategy backtester.

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
| `scan_patterns.py` | All 9 pattern scanners (incl. VCP, MA Squeeze) + `scan_date_range()` + `get_scan_results_range()` |
| `vcp_detector.py` | VCP (Volatility Contraction Pattern) detector — `detect_vcp(ticker, rows, config=None)`, called from `scan_patterns.py` |
| `volume_profile.py` | Volume Profile — volume-by-price histogram (POC + top-N support/resistance levels) cross-referenced against pivot-clustered "S/R channel" levels; `analyze(ticker, rows, config=None)`, called from `/api/volume-profile` |
| `surge_strategy.py` | Volume Surge strategy engine (Scenario A) — surge day → red-candle drop day → limit-buy at surge close → 15% partial + MA21 cut-loss. `scan_signals()` writes `surge_signals` (workflow states watching/armed/triggered/expired/bought/dismissed). Backs the "Surge Strategy" tab (tab 12). Also holds the **live monitor**: `start_monitor_thread()` (started by `api_server.py` on launch) runs `refresh_live()` every 30 min on US market hours (Mon-Fri 09:35-16:20 ET, plus one post-close pass at 16:20 and one startup pass), pulling Yahoo daily bars (~15 min delayed) for open positions + armed signals only. `evaluate_position()` applies the exit rule statelessly over the bars since the buy date (close < MA21, next open still < that MA21 -> SELL; +15% close -> sell half) — a small live-safe difference from the backtest: MA21 is taken as of the break day, not the confirmation day. Armed signals can trigger live (never downgraded by a later scan). Verified with `scripts/verify_surge_engine.py` (entries = backtest), `scripts/verify_surge_exit.py` (sell dates 93% identical, partial-target dates 100%) and `scripts/test_surge_monitor.py` (synthetic-bar + schedule tests). CLI: `python surge_strategy.py [--as-of DATE]` |
| `backtest_engine.py` | Pure-Python 3-stage exit strategy simulator (no GUI), used by `/api/backtest/*` |
| `market_health.py` | IBD-style Distribution Day / Follow-Through Day market health tracker (Nasdaq + S&P 500) |
| `universe.py` | Scrapes + caches the S&P 500 + Russell 1000 ticker universe (`ticker_universe` table); `get_active_universe()`, `refresh_universe()` (weekly), `get_universe_meta()` |
| `rs_calculator.py` | IBD-style RS Score/Rating/Line engine (`fetch_universe_prices()`, `compute_rs_ratings()`); writes `rs_ratings` |
| `backfill_rs.py` | Standalone historical RS Rating backfill (`python backfill_rs.py`) — separate from the daily incremental update wired into `fetch_data.py` |
| `Insider Trading/insider_pipeline/` | Standalone pipeline (SEC Form 4/6-K, Nasdaq Nordic, FI Insynsregistret) invoked by `/api/insider/scan`; writes into `insider_signals` |
| `congress_trades.py` | Congress (House + Senate) stock trade disclosure fetcher — `fetch_congress_trades()`, invoked by `/api/congress/fetch`; writes into `congress_trades` |
| `scan_cup_handle.py` | Original standalone Cup & Handle scanner (CLI only, not used by dashboard) |
| `templates/dashboard.html` | Entire frontend — HTML + CSS + JS in one file |
| `start.bat` | Double-click launcher |
| `stock_dashboard.db` | SQLite database (do not commit to git) |

## Database tables
| Table | Purpose |
|---|---|
| `stocks_daily` | OHLCV + indicators (MA6/10/30/50/100/150/200, High/Low 30D & 52wk, Vol MA10, RS Rank, IBD Trend Template criteria c1–c8 + trend_score) per ticker per date, for tracked `extraction_tickers` |
| `holdings` | Current positions |
| `sold` | Sold positions |
| `watchlists` | Named watchlists (id, name) — `id=1` is the default list |
| `monitor_list` | Watchlist entries, each tied to a `watchlist_id` |
| `extraction_tickers` | Tickers to fetch from Yahoo Finance |
| `pattern_scan_results` | Stored results from pattern scanner (keyed by scan_date + ticker + pattern_name) |
| `vcp_signals` | VCP detector output per ticker per scan_date (keyed by scan_date + ticker) — joined onto `pattern_scan_results` rows for the same ticker/date, regardless of which pattern fired |
| `skipped_stocks` | Tickers that failed/returned no data on last fetch — one row per ticker (upserted, not appended) |
| `insider_signals` | Insider Form 4/6-K buy/sell signals (cluster-buy + Rule 10b5-1 flags) |
| `congress_trades` | Congress (House + Senate) stock trade disclosures — cluster-buy flag, `dedup_key` UNIQUE for re-fetch dedup — see `congress_trades.py` |
| `surge_signals` | Volume Surge strategy signals (keyed by ticker + surge_date) with workflow `status`, drop_date, buy_level, window_days_left, trigger_date, last_price/price_asof/price_is_live (latest price from the live monitor, which now also watches triggered signals) — see `surge_strategy.py` |
| `surge_positions` | Positions the user marked as actually bought from a surge signal (partial/exit fields + live monitor fields: last_price, last_ma21, alert_state). Table exists; used from phase 2 |
| `ticker_universe` | S&P 500 + Russell 1000 constituents (`source`, `is_active`) — see `universe.py` |
| `universe_prices` | Full OHLCV + the same indicator/Trend Template columns as `stocks_daily`, for the S&P 500 + Russell 1000 universe (separate table from `stocks_daily`/`extraction_tickers`, but merged with it at read time in `/api/data/summary` — see Data tab below) |
| `rs_ratings` | RS Score/Rating (1-99)/Line/trend/`rs_leader` per ticker per date, computed against the full universe — see `rs_calculator.py` |
| `universe_meta` | Single-row admin table: last refresh date, sp500/russell1000/overlap counts, total active |

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
GET  /api/signals/ma100                 Price > MA100
GET  /api/signals/2xlow?multiplier=2.0  Price >= multiplier x all-time low
GET  /api/signals/2xlow/first?multiplier=2.0  first-ever 2x-low hit per ticker, tracked + full universe
GET  /api/signals/ma1030                MA10 > MA30
GET  /api/signals/ma200bear             Price < MA200 (bearish)
GET  /api/signals/ma1030bear            MA10 < MA30 (bearish)
GET  /api/signals/rsi?zone=oversold|overbought|neutral  RSI zone signals (omit zone for "All")
GET  /api/signals/all                   crossover/breakout types only — RSI excluded (see _ALL_VIEW_TYPES)

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

# Congress / political trading disclosures (House + Senate PTRs)
GET    /api/congress?ticker=&politician=&chamber=&party=&type=&from=&to=  (politician is a case-insensitive substring match)
GET    /api/congress/summary?month=YYYY-MM  or  ?month_from=&month_to=   top BUY/SELL tickers for a month or month range
DELETE /api/congress/<id>
POST   /api/congress/fetch              downloads latest snapshot, upserts, re-flags clusters
GET    /api/congress/fetch/status

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
POST /api/fetch                         {period?, skip_universe?} — starts background Yahoo Finance fetch
GET  /api/fetch/status
POST /api/us/quick-update               {scan?} FAST US update: last ~20 days for tracked + S&P500/Russell1000 tickers in batches (~70 s), RS refresh, then a Surge scan; shares the Fetch lock + log (poll /api/fetch/status)

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

# Data browser (tab 11) — tracked Ticker List + full S&P500/Russell1000 universe, merged
GET /api/data/summary                   latest snapshot per ticker (all indicator + RS columns), tracked tickers ∪ full active universe
GET /api/data/history?ticker=&limit=     full OHLCV+indicator history for one ticker (stocks_daily, falls back to universe_prices)
GET /api/debug/trend-template?ticker=    last 5 rows with Trend Template criteria breakdown

# Volume Profile (per-ticker volume-by-price + pivot-based S/R)
GET /api/volume-profile?ticker=&lookback=60&top_n=5   POC, top-N high-volume support/resistance
    levels, pivot-clustered "S/R channel" levels, and confirmed_levels (both agree — highest confidence)

# Universe admin (S&P 500 + Russell 1000 universe)
GET  /api/screener/meta                 universe admin info (active count, last refresh, source breakdown)
POST /api/universe/refresh              manual re-scrape (weekly auto-refresh piggybacks on /api/fetch)
```

```
# Surge Strategy (Volume Surge, Scenario A workflow) — see surge_strategy.py
GET    /api/surge/board                 signals (watching/armed/triggered), open positions, closed, counts, config
POST   /api/surge/scan                  scan stored daily data (last 20 trading days) + refresh existing signals
POST   /api/surge/signals/<id>/bought   {buy_price, buy_date?, shares?, note?} -> creates a surge_positions row
POST   /api/surge/signals/<id>/dismiss
POST   /api/surge/positions/<id>/partial {price, date?}   half sold at the +15% target
POST   /api/surge/positions/<id>/sell    {price, date?, reason?}  closes the rest
DELETE /api/surge/positions/<id>        undo a mistaken "I bought" (signal returns to the board)

# HK Surge (Hong Kong counterpart — separate DB/fetch/positions, see hk/hk_routes.py; all under /api/hk/)
POST   /api/hk/fetch                    {mode: quick|full|shares} background HK fetch — never touches US data (the header Fetch button is US-only)
GET    /api/hk/fetch/status?since=N     running/ok/error + log lines
GET    /api/hk/board                    signals, positions, closed, alerts, monitor, account + sizing suggestions
POST   /api/hk/scan                     scan stored HK data (mcap >= HK$1B, turnover >= HK$5M/day) for new signals
POST   /api/hk/signals/<id>/bought      {buy_price, shares? | amount?, buy_date?, note?}   POST /api/hk/signals/<id>/dismiss
POST   /api/hk/positions/<id>/partial | /sell     DELETE /api/hk/positions/<id>
GET    /api/hk/alerts   GET /api/hk/monitor/status   POST /api/hk/monitor/refresh
GET    /api/surge/alerts                actionable alerts (sell / MA21 warn / +15% target / new buy signal) + monitor status; the page polls it every 60 s from any tab
GET    /api/surge/monitor/status        last/next live pass, market open, last error
POST   /api/surge/monitor/refresh       run one live pass now (Yahoo) — the "Live Refresh" button
```

## Dashboard tabs (in order)
1. **Holdings** — portfolio positions with live P/L + Cut Off price column
2. **Signals** — MA200, MA100, and MA10>MA30 breakout signals (bull + bear, all triggered off
   **close** price), Price≥Nx-Low ("doubled from its low", adjustable multiplier, optional "first
   time only / full universe" one-row-per-ticker mode), and RSI zone (Oversold/Overbought/Neutral/All)
   — one unified table, date range filter, RS Rank range and vs-52wk-High% range filters. "Day Low"/
   "Day High" columns are informational only (the day's actual intraday range) — no signal type's
   trigger condition uses them; a Low/High-based variant of MA200/MA100 was tried and rolled back
   (see git history) since it meant a signal could only confirm *after* a big move already happened
   (e.g. a large gap-up), rather than catching a stock testing/approaching the MA in advance — that
   "near MA200" early-warning idea was requested but not yet built. RSI is excluded from the "All"
   combined view (see `_ALL_VIEW_TYPES` in `api_server.py`) since it's a level, not a crossover event,
   and would flood it.
3. **Sold** — realised P/L history
4. **Monitor** — named watchlists with signal price tracking; per-row `+ Backtest` button
5. **Pattern Scanner** — 9 pattern scans (incl. VCP, MA Squeeze), single-date or date-range scan, ticker filter, VCP-only filter, per-row `+ Backtest` button
6. **Ticker List** — manages extraction_tickers (what gets fetched)
7. **Backtester** — strategy backtester with selection queue
8. **Insider** — insider Form 4/6-K buy/sell signals, cluster-buy detection, on-demand scan by ticker + date range
9. **Congress** — House + Senate + executive branch (President/VP/Cabinet) stock trade disclosures (PTRs / OGE 278-T), cluster-buy detection, "Fetch Data" downloads the latest snapshot
10. **Market** — IBD-style Distribution Day / Follow-Through Day market health (Nasdaq + S&P 500), status banner + 60-day chart
11. **Data** — combined data browser + RS Rating screener: tracked Ticker List *and* the full S&P 500 + Russell 1000 universe in one table (latest snapshot, per-ticker history, Trend Template criteria, RS Rating/Score/Line, "Tracked only" + RS Rating ≥ filters, universe admin card)
12. **Surge Strategy** — workflow board for the Volume Surge strategy: Buy Signals (limit price reached; "I bought" / Dismiss) (every signal row shows **Price now** — the live monitor's price when it has one, else the latest stored close — and **vs limit / vs level**: amber when the price is already ABOVE the buy level, i.e. a buy order at that level would not fill right now) → **Drop Day** (first red candle found, buy level armed for 5 trading days; a blue 🔻 DROP DAY alert + NEW badge fires for a fresh one; `status='armed'` in the DB) → Watching (surge found, no red candle yet), then My Positions (marked as bought; +15% target, last close vs MA21, P/L, "Half sold"/"Sold"/"Undo") and Closed. A triggered signal goes stale 3 trading days after its trigger day. **Quick US update** (~70 s: last 20 days, batched, then scans) is the fast way to bring the data current — the header **Fetch US Data** is the slow full 2-year refresh; "Scan Now" only reads stored daily data (so it needs one of them first) to find new surge signals; "Live Refresh" (and the automatic 30-min monitor while `api_server.py` runs) updates positions with live prices. A scan (Scan Now, or the scan after a fetch) **ignores today's still-forming daily bar until the close** (16:20 ET / 16:20 HKT) — a fetch made while the market is open stores a partial bar whose red candle can still turn green (`drop_forming_bars()`), so a drop day is only ever taken from a closed candle. The live monitor also watches the "Watching" signals and, once a red candle has CLOSED (after 16:20 ET; never from a still-forming candle), promotes them to Drop Day the same evening without waiting for a Fetch + Scan. An alert banner lists SELL / MA21-warning / +15% / new Buy Signal / new Drop Day items, the tab shows an alert-count badge, and "Enable alerts" turns on browser notifications (page must stay open).

13. **HK Surge** — the same Volume Surge workflow for Hong Kong stocks, fully separate from tab 12: its own data (`hk/hk_stocks.db`), its own fetch buttons (**Fetch HK Data (quick)** ≈ 40 s for the ~800 stocks passing the size/liquidity screen, **Full refresh** ≈ 5 min for all ~2,800 HKEX equities, **Refresh shares**), its own positions and its own live monitor on Hong Kong hours (09:45-16:20 HKT, lunch skipped, every 30 min). Signals are filtered to market cap >= HK$1B and turnover >= HK$5M/day (both measured the day before the surge). Same Drop Day stage, alert and Price now / vs level columns as the US tab (promoted after 16:20 HKT). Buy Signals show a suggested size from the account model: 20 slots x HK$10,000, profit shared evenly over free slots, one position <= 10% of equity, ~0.32% round-trip costs, rounded down to whole board lots. Positions take an HK$ amount or shares so the account (cash, free slots, next stake, realized P/L) can be tracked.

`TABS` array in `dashboard.html` (`['holdings','signals','sold','monitor','patterns','tickers','backtester','insider','congress','market','dataview','surge','hk']`) order must match the HTML tab-btn order. There is no standalone RSI Signals tab — it was merged into Signals.

## Holdings — Cut Off Price column
Calculated per row, shown in red:
- **At a loss** (current price ≤ avg buy): `avg_buy_price × 89%`
- **In profit** (current price > avg buy): `high_30d × 89%`

## Named Watchlists (Monitor tab)
- `watchlists` table holds named lists; `monitor_list.watchlist_id` ties each entry to one (default `id=1`).
- `GET/POST/DELETE /api/watchlists` manage the lists themselves; `/api/monitor?watchlist=<id>` filters entries.
- Deleting a watchlist cascades and deletes its `monitor_list` entries.

## Pattern Scanner (tab 5)
- 9 patterns defined in `scan_patterns.py` with tuneable CONFIG at top of file (VCP's config lives in `vcp_detector.py`)
- **Universe: the full active S&P 500 + Russell 1000 universe (~1,139 tickers), not just the tracked
  Ticker List.** `_load_prices_up_to()` merges `stocks_daily` (tracked) with `universe_prices` (the
  rest of the active `ticker_universe`), tracked wins on overlap — same rule `/api/data/summary` uses
  for the Data tab. `get_scan_results()`/`get_scan_results_range()`'s `_TT_JOIN`/`_TT_COLS` apply the
  same tracked-then-universe fallback so Trend Template/RS/52wk columns are populated for untracked
  universe tickers too (`stocks_daily` alone would leave those columns NULL for them). A single-date
  scan over the full universe runs in a few seconds; a several-week range scan in low tens of seconds
  — no meaningful performance concern at current universe size. (Before this, the scanner only ever
  saw the ~350 tracked tickers — a stock only on the S&P500/Russell1000 universe, not the Ticker List,
  would never appear no matter what it matched.)
- **Single scan**: POST `/api/patterns/scan` with `{date}` — scans one trading day
- **Range scan**: POST `/api/patterns/scan` with `{from_date, to_date}` — scans every trading day in range; loads full price history once for efficiency
- Results stored in `pattern_scan_results` table (re-scanning same date overwrites); VCP detail additionally stored in `vcp_signals`
- Frontend polls `/api/patterns/scan/status` for progress
- Results tab: single-date dropdown OR date range viewer (Load / Clear)
- Ticker filter input + "VCP only" checkbox: live filters the results table client-side
- `VCP` and `vs Pivot` columns are sortable (click header, same mechanism as the Data tab)
- Tab state (results, filter, sub-tab) is preserved across tab switches; only fetches on first visit
- `+ Watch` button adds to Monitor (asks which named watchlist); `+ Backtest` button adds to Backtester queue
- **Quick Multi-Pattern Filter**: a checkbox panel (all 9 patterns — Cup & Handle / Golden Cross /
  MA200 Breakout / Volume Surge / Pullback Bounce / Momentum Expansion / Momentum 10/8 / MA Squeeze /
  VCP — plus an optional "Price Rise ≥ N%") below the Trend Template filter panel. Entirely client-side over
  whichever rows are already loaded via the date-range "View range" picker (the "Quick Filter" button
  just calls `loadPatternRange()` then re-applies) — no new backend endpoint. Groups the loaded
  `pattern_scan_results` rows by ticker and keeps only tickers where **every checked pattern fired at
  least once somewhere in the range** (VCP counts via the same `vcp_signals` join the "VCP only"
  checkbox uses, so it matches even when VCP shows up as a badge on a non-VCP-named row; MA Squeeze
  respects the "Fresh only" toggle). "Price Rise" compares each matching ticker's `close` on its
  earliest vs. latest matched row in the range — not a full daily series, just the pattern-hit
  bookends — so it's a quick screen, not an exact period return. Swaps the results table header
  (`_setPatternsHeaderMode`) to a per-ticker summary (Patterns Matched / First Hit / Last Hit / Start
  Close / Latest Close / Price Chg % / RS / Trend) while any box is checked, and restores the normal
  per-signal-row header when all are unchecked. Still respects the ticker-text filter, Monitor-list
  filter, and Trend Template C1–C8 filter (checked against the ticker's latest matched row).

### Pattern rules (summary)
1. **Cup & Handle** — 60-day cup + 10-day handle + breakout above rim with vol > 1.5× MA10
2. **Golden Cross** — MA10 crosses above MA30 (Signal A) and/or MA50 crosses above MA200 (Signal B)
3. **MA200 Breakout** — close crosses above MA200, vol > 1.2× MA10, MA50 > MA200
4. **Volume Surge** — price up, vol ≥ `VOL_SURGE_MULT` (default 2.0)× MA10, breaks above High_30D,
   direction = Up. The multiplier is tuneable per-scan from the "Vol Surge ×" input next to Squeeze
   %/Price %/Max Age in the Pattern Scanner toolbar (`vol_surge_mult` on `POST /api/patterns/scan`,
   threaded through `scan_all_patterns()`/`scan_date_range()` the same way the MA Squeeze thresholds
   are — falls back to `VOL_SURGE_MULT` when omitted).
5. **Pullback Bounce** — close within 5% above Low_30D, above MA200, price bouncing, MA10 > MA30
6. **Momentum Expansion** — ≥12 of last 15 days up, close gained ≥20% over the 15-day window, recent 15D avg volume ≥1.25× the prior 15D window
7. **Momentum 10/8** — ≥8 of last 10 days up, recent 10D avg volume ≥1.25× the prior 10D window
8. **VCP (Volatility Contraction Pattern)** — see below
9. **MA Squeeze** — MA9/21/50/100 convergence, see below

### MA Squeeze detector (`scan_patterns.py::_ma_squeeze`)
- Distinct from VCP: VCP looks at swing-high/low price contraction; this looks purely at moving-average convergence.
- Uses 4 simple MAs of close price: `SQUEEZE_MA_PERIODS = (9, 21, 50, 100)`. Needs `max(periods) + SQUEEZE_LOOKBACK_DAYS` (160) days of history or the ticker is skipped.
- **Squeeze condition**: `spread_pct = (highest MA − lowest MA) / lowest MA × 100` must be ≤ `SQUEEZE_THRESHOLD` (default 3.0%) — the 4 MAs are tightly clustered.
- **Price-proximity condition**: close must be within `PRICE_THRESHOLD` (default 2.0%) of the MA cluster's midpoint (`(highest + lowest) / 2`) — price hasn't already run away from the converging MAs.
- Both gates must pass; there's no separate volume/tightness gate like VCP has.
- `squeeze_days`: consecutive trading days (counting back from today) the spread has stayed ≤ threshold.
- `squeeze_fresh`: 1 when `squeeze_days ≤ MAX_SQUEEZE_AGE` (default 10) **and** the spread is still tightening — mean spread over the last `SQUEEZE_TIGHTEN_RECENT_DAYS` (5) days is narrower than the mean over the prior `SQUEEZE_TIGHTEN_PRIOR_DAYS` (20, i.e. days 6–20 back). A long-running or already-widening squeeze is not "fresh."
- Stored in `pattern_scan_results` (`squeeze_spread_pct`, `squeeze_days`, `squeeze_fresh` columns) like every other pattern — no separate detail table (unlike VCP's `vcp_signals`).

### VCP detector (`vcp_detector.py`)
- Prerequisite: skips tickers whose latest Trend Template `trend_score` is below `VCP_MIN_TREND_SCORE` (default 6/8); rows with no trend_score yet are allowed through rather than dropped.
- Finds swing highs/lows with a simple N-day pivot window (`VCP_PIVOT_WINDOW`, default 5) — no scipy dependency.
- Builds peak→trough pullback legs, keeps the most recent `VCP_MAX_LEGS` (default 3), and walks backward from the latest leg keeping earlier legs only while each is ≥`VCP_CONTRACTION_TOLERANCE` (default 20%) deeper than the one after it.
- `vcp_detected = True` when that contraction chain has ≥`VCP_MIN_CONTRACTIONS` (default 2) legs — this is the only detection gate; volume dry-up and tightness are reported separately for the user to filter/sort on, not gates.
- `volume_dryup`: each leg's average volume must be non-increasing across the chain AND the final leg's average volume must be below the trailing 50-day average (× `VCP_VOLUME_DRYUP_RATIO`).
- `tightness_pct` / `pivot_price`: high-low range over the last `VCP_TIGHTNESS_DAYS` (default 10) as a % of price; pivot = high of that window.
- `suggested_stop = pivot × (1 − VCP_STOP_PCT)` (default 7%); `current_price_vs_pivot` = % distance of close from pivot (closest-to-breakout candidates sort near 0).
- `detect_vcp(ticker, rows, config=None)` takes the same row-dict-list shape as the other scanners in `scan_patterns.py` (not a DataFrame), for consistency with the rest of the file.
- Every ticker that passes the prerequisite filter gets a `vcp_signals` row (detected or not); only `vcp_detected=True` tickers also get a `pattern_scan_results` row with `pattern_name='VCP'`. The `vcp_signals` join in `get_scan_results()`/`get_scan_results_range()` means the VCP badge/pivot/stop columns show up on *any* pattern row for that ticker/date, not just VCP's own rows.

## Volume Profile module (`volume_profile.py`)
Not a pattern scanner (no `pattern_scan_results` row, no scan-all/scan-date-range job) — a per-ticker,
on-demand analysis surfaced only via `GET /api/volume-profile?ticker=&lookback=&top_n=` and the Data
tab's ticker History modal (see below). No pivot-based support/resistance module existed anywhere in
this codebase before this; `pivot_levels()` below is a new companion detector alongside the volume
histogram, not a pre-existing "SRchannel" feature being wired up.
- **Volume levels**: bins the lookback window's (default `VP_LOOKBACK_DAYS`=60 trading days) full
  low–high price range into `VP_NUM_BINS` (default 24) equal-width bins, and distributes each day's
  volume across the bins its `[low, high]` range overlaps, proportional to overlap width (the
  standard no-tick-data volume-profile approximation — true tick-level profiles aren't derivable from
  daily OHLCV). Index-adjacent high-volume bins are greedily merged into one level (`_merge_adjacent_bins`)
  so a single peak straddling a bin boundary isn't reported twice. **POC** (point of control) = the
  single raw bin with the most volume; **top N levels** (`VP_TOP_N`, default 5) = the merged levels
  ranked by total volume. Each level is classified `support` (price ≤ current close) or `resistance`
  (price > current close).
- **Pivot levels** (`pivot_levels()`): reuses the same N-day swing-high/low pivot window as
  `vcp_detector.py` (`VP_PIVOT_WINDOW`, default 5, over a longer `VP_PIVOT_LOOKBACK_DAYS`=120 window —
  pivots need more room to form than the volume histogram does), but — unlike VCP, which chains
  peak→trough pullback legs — clusters ALL swing points (highs and lows alike) into price bands by
  proximity (`VP_PIVOT_CLUSTER_PCT`, default 1.5%; classic "S/R channel" technique). A cluster needs
  ≥`VP_PIVOT_MIN_TOUCHES` (default 2) swing-point touches to count; strength = touch count. Classified
  support/resistance the same way as volume levels.
- **Cross-reference**: a volume level and a pivot level within `VP_CONFIRM_TOLERANCE_PCT` (default
  1.5%) of each other are the same real-world level seen two ways — the volume level is flagged
  `confirmed_by_pivot=True`, `confidence="high"`, and included in `confirmed_levels` (the endpoint's
  highest-confidence support/resistance output). Unconfirmed levels stay `confidence="volume_only"`.
- `analyze(ticker, rows, config=None)` is the single entry point, mirroring `detect_vcp`'s shape/config
  pattern; `config` overrides merge onto `DEFAULT_CONFIG`. All thresholds above are tuneable at the top
  of `volume_profile.py`.

## Insider tab
- Backed by `insider_signals`, populated by running `Insider Trading/insider_pipeline/main.py` as a subprocess via `POST /api/insider/scan {tickers, from_date, to_date}`.
- Sources: SEC Form 4, Form 6-K, Nasdaq Nordic filings, FI Insynsregistret (Sweden) — captures both buys and sells.
- Flags cluster buys (`cluster_buy`) and Rule 10b5-1 pre-scheduled sales (`flag_10b51`).
- `GET /api/insider/scan/status` polls background scan progress/log; scans are serialized (409 if already running).

## Congress tab
- Backed by `congress_trades`, populated by `congress_trades.py:fetch_congress_trades()` via `POST /api/congress/fetch` (in-process background thread, like `/api/fetch` — not a subprocess like the Insider pipeline).
- **Data source**: the two originally-planned free sources (House Stock Watcher's S3 bucket, Senate Stock Watcher's site) are both dead — S3 returns `AccessDenied`, `senatestockwatcher.com` no longer resolves. Replaced with [kadoa-org/congress-trading-monitor](https://github.com/kadoa-org/congress-trading-monitor)'s `public/data/trades.json` on GitHub (`raw.githubusercontent.com`, no auth), an actively-maintained mirror scraping the same official primary sources (House Clerk's disclosure site + Senate eFD) those dead projects used to, plus OGE Form 278-T filings for the executive branch.
- **Scope: "political trading," not just Congress** — covers all three source_ids in the feed: `house_clerk`→chamber `House`, `senate_efd`→chamber `Senate`, `oge_executive`→chamber `Executive` (President/VP/Cabinet/agency heads; `source`=`OGE-278`). `party`/`state` are always NULL on Executive rows upstream (not fabricated); `state_or_district` falls back to the `agency` field (e.g. `"White House Office"`) for those. A given official (e.g. a VP) only appears once a PTR of theirs has actually been scraped upstream — absence isn't a filter bug on this end.
- It's a **rolling/recent snapshot** maintained upstream, not a full historical archive — same "recent activity" scope as the Insider tab.
- Normalizes `transaction_type` → `type` (`Purchase`→`BUY`, `Sale (Full|Partial)`→`SELL`, `Exchange`→`EXCHANGE`); `amount_range_label` is kept as free text (`amount_range`, e.g. `"$1,001 - $15,000"`) — deliberately not coerced into a numeric total, since PTR filings only disclose a bracket, not an exact value.
- `role` / `state_or_district` are parsed from the upstream `office` field — Congress: `"U.S. Representative · WA-01"` → role=`"U.S. Representative"`, state_or_district=`"WA-01"`; Executive: no `·` delimiter, so the whole string (`"President"`, `"Secretary"`, ...) is the role and state_or_district falls back to `agency`.
- **Dedup**: `dedup_key` (politician + ticker + tx_date + amount_range, matching the originally-requested key) is a `UNIQUE` column; re-fetching uses `INSERT OR IGNORE` (same upsert style as `insider_signals`/`db_writer.py`) so re-running the fetch is always safe. Note: repeat line-items within one filing that share all four fields (rare but possible) collapse to one row under this key.
- **Cluster detection**: `cluster_buy` flag, recomputed in full on every fetch — 3+ distinct politicians trading the same ticker in the same direction (BUY/SELL/EXCHANGE) within 7 days of each other (same thresholds as `Insider Trading/insider_pipeline/config.py`'s `CLUSTER_WINDOW_DAYS`/`CLUSTER_MIN_INSIDERS`, reimplemented as a SQL post-pass in `congress_trades.py:_flag_clusters()` rather than importing that pipeline's pandas-based detector, since that package is meant to run standalone/via subprocess).
- Frontend polls `GET /api/congress/fetch/status` for progress/log, same pattern as the Insider scan; ticker/politician/chamber/party/type filters + date range, `+` delete per row. The politician filter box applies on Enter (not live/on-input like ticker), matching the "type a name, press Enter" UX explicitly asked for — backed by a plain `LIKE '%term%'` substring match server-side.
- **Monthly summary panel** (above the main table): pick a single month or an inclusive month range (native `<input type=month>`, defaults to the current calendar month on first tab visit) and see the top-10 most-bought and top-10 most-sold tickers for that period (by trade count, tie-broken by distinct-politician count), plus BUY/SELL/distinct-ticker/distinct-politician summary cards. `GET /api/congress/summary` computes the date bound as `[month_from-01, first-day-of-month-after-month_to)`. Clicking a ticker in either list filters the main table below to it.

## Market tab (Distribution Day / Follow-Through Day)
- `market_health.py` fetches ^IXIC (Nasdaq) and ^GSPC (S&P 500) via yfinance and classifies market health IBD-style.
- **Distribution Day**: close down ≥0.2% on higher volume than prior day; expires after 25 trading days or a 5%+ recovery above that day's close.
- **Follow-Through Day**: on rally day 4–20 (from most recent low), close up ≥1.25% on higher volume.
- **Status** (higher of the two indices' active DD counts): 0–3 Healthy, 4–5 Caution, 6+ Correction Likely, 6+ with a recent FTD → New Uptrend Confirmed (override).
- Result cached in memory per calendar day; `GET /api/market/status?refresh=1` forces a re-fetch.
- Tuneable thresholds live at the top of `market_health.py`.

## RS Rank / IBD Trend Template (fetch pipeline)
- `fetch_data.py` computes MA150, 52-week high/low, and a raw Relative Strength score during fetch, then ranks tickers into `rs_rank` (percentile) — self-relative, only among currently-tracked `extraction_tickers`.
- Also evaluates IBD's 8-criteria Trend Template (columns `c1`–`c8`, aggregate `trend_score`) per ticker per day, e.g. Close > MA150 & MA200, MA150 > MA200, MA200 trending up, Close > MA50, RS Rank ≥ 70, within 25% of 52-week high, etc.
- **C7 now prefers the real market-wide `rs_ratings.rs_rating`** (see below) wherever a matching ticker+date row exists, falling back to the self-relative `rs_rank` otherwise — same fallback applied to the `RS` column shown on Signals/Pattern Scanner/Data (via `COALESCE(rs_ratings.rs_rating, stocks_daily.rs_rank)`), so the number and the C7 badge never disagree.
- **`quick_update()` / `python fetch_data.py --quick` / `POST /api/us/quick-update`**: downloads only the last 20 days for every tracked ticker + the active universe + the RS benchmark in batches of 200 (`yf.download`), splices onto the stored history, recomputes indicators for the recent rows only (`since=` argument on `_compute_universe_indicators`, `_compute_rs_rank`, `_compute_trend_template`), refreshes `rs_ratings` for the new dates, RS Rank and the Trend Template — the same values the full Fetch produces for those dates (verified by deleting two days and rebuilding them), in about a minute instead of a full refresh. Tracked tickers with no stored history are skipped (need one full Fetch). The US Surge tab uses US trading dates only (`.HK` tracked tickers are excluded from its date logic).
- **History is spliced, not replaced**: every Fetch downloads only `period` (default 2y) but `_splice_stored_history()` prepends the OHLCV already stored in `stocks_daily` before computing indicators, so MA150/MA200, 52-week range and RS never fall back to a 2-year window (before this, each Fetch left MA200 blank for the first 200 days of its window and silently dropped older signals/backtest trades). Only the freshly downloaded rows are written back. To repair rows after a bad run — no download — use `python fetch_data.py --recalculate` (or `recalculate_indicators()`). Caveat: prices are auto-adjusted, so after a dividend the seam between old and new rows can differ slightly.
- Inspect via `GET /api/debug/trend-template?ticker=` (last 5 rows + criteria breakdown) or the Data tab.

## Data tab (tab 11) — tracked Ticker List + full S&P 500/Russell 1000 universe, merged
The Data tab and the old standalone "Screener" tab were merged into one table: `GET /api/data/summary`
returns tracked `stocks_daily` rows unioned with the full active universe's `universe_prices` rows
(tracked tickers win on overlap — see `tracked` field per row), both carrying the same OHLCV/MA/Trend
Template/RS columns so no per-source column gaps exist in the UI.
- **Universe** (`universe.py`): scrapes S&P 500 + Russell 1000 constituents from Wikipedia (`pd.read_html`, needs `lxml`), unions/de-dupes into `ticker_universe`. Tickers no longer present are marked `is_active=0`, never deleted, so historical `rs_ratings` stay intact. `get_active_universe()` warns (`RS_UNIVERSE_SIZE_WARNING`, default 200) if the active set is too small. Refresh cadence is weekly (`UNIVERSE_REFRESH_INTERVAL_DAYS`, tracked via `universe_meta.last_refresh_date`) — there is no scheduler in this codebase, so the refresh check runs as part of `fetch_data.py:fetch_all()` (piggybacks on the existing Fetch button/`python fetch_data.py`), not on its own timer.
- **Price + indicator cache** (`rs_calculator.py:fetch_universe_prices` + `fetch_data.py:_compute_universe_indicators`): `universe_prices` stores full OHLCV (batched `yf.download()`, 200 tickers/chunk, threaded — each chunk wrapped in try/except so one bad batch doesn't abort the run) plus the same MA/52wk/Trend-Template columns `stocks_daily` has, computed via the same ticker-agnostic `_calculate_indicators()` used for tracked tickers. Runs every Fetch by default (adds meaningful time — OHLCV download itself is cheap/batched, but per-ticker indicator computation across ~1,000 tickers is a Python loop). The Fetch modal has a "Skip universe refresh" checkbox (`skip_universe` on `POST /api/fetch`, `--skip-universe` CLI flag on `fetch_data.py`) for a faster tracked-tickers-only run — RS Ratings/universe data simply stay at their last-fetched values until the next full run.
- **RS Score/Rating/Line** (`rs_calculator.py:compute_rs_ratings`): weighted 3/6/9/12-month performance (63/126/189/252 trading days; 0.4/0.2/0.2/0.2), vectorized across the whole date×ticker price matrix at once. Stocks with <252 trading days of history get a NULL score for that date (skipped, not errored). RS Rating = cross-sectional `rank(pct=True)*98+1`, rounded, clipped to [1,99] — always computed against the *entire* active universe on one date, never a single ticker in isolation. RS Line = Close / benchmark Close (`RS_BENCHMARK`, default `^GSPC`; QQQ/IWM supported via the `benchmark` param); `rs_line_trend` = 'up'/'down' vs. 20 trading days ago; `rs_leader` = 1 when `rs_rating>=70 AND rs_line_trend='up'`. One shared function drives both the historical `backfill_rs.py` (`dates=None` → every date in the matrix) and the daily incremental step in `fetch_data.py` (`dates=[today]`).
- **Limitation** (documented at the top of `rs_calculator.py`): percentile is relative to the tracked ~1,000-stock universe, not IBD's full ~9,000-stock US market, and historical backfill applies *today's* index membership retroactively (no point-in-time reconstitution data).
- **UI**: sortable Ticker/Date/Close/Day%/Volume/MAs/52wk/RS Rating/RS Score/Trend columns, inline-SVG RS Line sparkline (no Chart.js — one canvas per row would be unprecedented for this app), RS Rating ≥ threshold filter, ticker filter, "Tracked only" checkbox (small green dot marks tracked-list tickers in the Ticker column), Trend Template C1-C8 filter panel, and an admin card (active count, last refresh date, sp500/russell1000/overlap counts) with a manual "Refresh Universe Now" button (also refreshes the table). The shared `_ttBadge()` renderer (also used by Signals/Pattern Scanner) shows an "RS Leader" tag wherever `rs_leader === 1`.
- **Volume Profile panel**: the per-ticker History modal (opened via a row's history button) has a
  "Volume Profile & S/R Levels" section below the price-history table (lookback + top-N inputs, Load
  button) that calls `GET /api/volume-profile` — see the Volume Profile module section above. Shows the
  current price/POC, the top volume levels table, the pivot-based S/R channels table, and — highlighted
  — any levels confirmed by both (`confirmed_levels`).

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

## Backup / Restore
- `GET /api/backup/download` streams `stock_dashboard.db` as a file attachment.
- `POST /api/backup/restore` accepts a multipart `file` upload, validates the SQLite header magic bytes, then atomically replaces the live DB (write to temp file, `os.replace`).

## Frontend notes
- All JS/CSS is inline in `templates/dashboard.html` (no build step)
- Tab switching: `showTab(name)` — `TABS` array (13 items) order must match HTML tab-btn order
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
- `hk/` — the Hong Kong module, isolated from the US code: own database `hk/hk_stocks.db` (`hk_daily`, `hk_securities` incl. shares outstanding + board lot, `hk_surge_signals`, `hk_surge_positions`). `hk_fetch.py` (CLI: default Hang Seng Index, `--all` every HKEX equity, `--quick` daily update, `--shares`), `hk_study.py` (backtest + capital simulation with `--min-mcap`, `--min-turnover`, `--slots`, `--max-pos-pct`, `--costs`, `--max-positions`, `--entry-mode optimistic|stop|limit`, `--data-through`), `hk_start_sweep.py` (start-month sensitivity), `hk_surge.py` (live engine: scan + Hong Kong monitor; reuses the pure functions of `surge_strategy.py`), `hk_routes.py` (Flask blueprint behind the HK Surge tab, registered by `api_server.py`), `test_hk_surge.py`. `hk/runs/` (generated) is git-ignored. Only `api_server.py` imports `hk_routes`/`hk_surge`; nothing in `hk/` reads or writes `stock_dashboard.db`. Known bias: the ticker list is today's listings (survivorship). A liquidity filter is essential for the full market (unfiltered, the study is dominated by untradeable penny stocks).
- **Backtest fill realism**: the surge studies (`scripts/study_volume_surge_2026_04.py`, `hk/hk_study.py`) default to `ENTRY_MODE="optimistic"` — the Scenario A buy is booked exactly at the surge-day close whenever the trigger day's High reaches it, even if the stock never traded down to it (35-40% of trades). That overstates results ~3x. `ENTRY_MODE="stop"` pays max(open, level) on the way up; `"limit"` needs Low <= level and pays min(open, level). `scripts/study_entry_modes.py` runs the US study in all three, optionally over several volume multiples with `--vol-mults` (output `us_entry_runs/vm<X>/<mode>/`, git-ignored); `hk_study.py --entry-mode` does HK. Quote stop/limit numbers, not optimistic ones.
- The `Football Video/` directory is an unrelated project that happens to live under this path; ignore it for dashboard work.
