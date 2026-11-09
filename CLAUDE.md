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

Remote (fixed Tailscale Funnel address, set as DASH_URL in the git-ignored remote_login.bat): `start_tailscale.bat` (Basic Auth from git-ignored `remote_login.bat`, Tailscale Funnel on port 5000; exits with a message if port 5000 is already in use). The Windows task **"StockDashboard Start at Logon"** runs it automatically 30 s after logon in a minimized "Stock Dashboard" window (closing that window stops the dashboard and its Surge/Intraday monitors).

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
| `weekly_stats.py` | Weekly up/down counter — `weekly_up_down(ticker, start, end)` pulls live Yahoo `1wk` bars, classifies each week up (Close > Open) / down (Close < Open) / flat, compares avg + total volume on up vs down weeks, finds the highest-volume week, flags partial edge weeks; pure `summarize_weeks(weeks)` does the math. Backs `/api/weekly-stats` and the Weekly tab (tab 14). CLI: `python weekly_stats.py VKTX 2025-04-07 [END]` |
| `surge_strategy.py` | Volume Surge strategy engine (Scenario A) — surge day → red-candle drop day → limit-buy at surge close → 15% partial + MA21 cut-loss. `scan_signals()` writes `surge_signals` (workflow states watching/armed/triggered/expired/bought/dismissed). Backs the "Surge Strategy" tab (tab 12). Also holds the **live monitor**: `start_monitor_thread()` (started by `api_server.py` on launch) runs `refresh_live()` every 30 min on US market hours (Mon-Fri 09:35-16:20 ET, plus one post-close pass at 16:20 and one startup pass), pulling Yahoo daily bars (~15 min delayed) for open positions + armed signals only. `evaluate_position()` applies the exit rule statelessly over the bars since the buy date (close < MA21, next open still < that MA21 -> SELL; +15% close -> sell half) — a small live-safe difference from the backtest: MA21 is taken as of the break day, not the confirmation day. Armed signals can trigger live (never downgraded by a later scan). Verified with `scripts/verify_surge_engine.py` (entries = backtest), `scripts/verify_surge_exit.py` (sell dates 93% identical, partial-target dates 100%) and `scripts/test_surge_monitor.py` (synthetic-bar + schedule tests). CLI: `python surge_strategy.py [--as-of DATE]` |
| `backtest_engine.py` | Pure-Python 3-stage exit strategy simulator (no GUI), used by `/api/backtest/*` |
| `market_health.py` | IBD-style Distribution Day / Follow-Through Day market health tracker (Nasdaq + S&P 500) |
| `universe.py` | Scrapes + caches the S&P 500 + Russell 1000 ticker universe (`ticker_universe` table); `get_active_universe()`, `refresh_universe()` (weekly), `get_universe_meta()` |
| `rs_calculator.py` | IBD-style RS Score/Rating/Line engine (`fetch_universe_prices()`, `compute_rs_ratings()`); writes `rs_ratings` |
| `backfill_rs.py` | Standalone historical RS Rating backfill (`python backfill_rs.py`) — separate from the daily incremental update wired into `fetch_data.py` |
| `Insider Trading/insider_pipeline/` | Standalone pipeline (SEC Form 4/6-K, Nasdaq Nordic, FI Insynsregistret) invoked by `/api/insider/scan`; writes into `insider_signals` |
| `congress_trades.py` | Congress (House + Senate) stock trade disclosure fetcher — `fetch_congress_trades()`, invoked by `/api/congress/fetch`; writes into `congress_trades` |
| `nightly_collect.py` | Nightly data collector, run by the Windows scheduled task **"StockDashboard Nightly Collect"** daily at 23:00 (pythonw, start-when-available if the PC was off; works without the dashboard running). (1) Congress/executive trades via `fetch_congress_trades()` — the upstream feed is a rolling snapshot, so nightly runs are what build lasting history; (2) SEC Form 4 for the WHOLE market (insider pipeline market mode, `--days 3 --store-all`): every transaction into `insider_trades_all`, the filtered high-conviction ones into `insider_signals`, then whole-table cluster flags recomputed on `insider_signals`; (3) SEC Form 144 + Schedule 13D/13G for the last 3 filing days via `sec_filings.collect()`. The SEC steps run sequentially (shared 8 req/s throttle in the pipeline's `sec_fetcher`). The task runs with `--wait-lock-hours 16`: the lock holds the running PID, and a run that finds another active one waits instead of skipping (stale = PID gone). Lock file + log in `logs/` (git-ignored), one row per job in `data_collection_runs`. `--backfill-days N` (insider, 14-day slices saved one by one) / `--backfill-sec-days N`, `--skip-insider` / `--skip-congress` / `--skip-sec` |
| `nightly_scan.py` | Nightly **Pattern Scanner** job — Windows task **"StockDashboard Nightly Scan"**, Mon-Fri 23:15 (pythonw, start-when-available). Waits for `nightly_collect.py` (max 90 min), then quick US price update + Surge scan (`daily_brief.refresh_data`) + `scan_patterns.scan_all_patterns()` for the latest complete session (default thresholds) -> `pattern_scan_results` / `vcp_signals`, then the Factor & Sector Rotation refresh (`rotation.refresh()`, detail key `rotation`). One `data_collection_runs` row (job `pattern_scan`, detail: as_of, pattern_hits, vcp_detected, minutes). Lock `logs/nightly_scan.lock` (PID), log `logs/nightly_scan.log`. The Morning Brief waits for it and, when it succeeded in the last 6 h, skips its own update + scan (falls back to doing both if it didn't run). CLI: `python nightly_scan.py [--as-of DATE] [--no-update]` |
| `daily_brief.py` | Nightly **Morning Brief** — Windows task **"StockDashboard Daily Brief"**, Mon-Fri 23:30 (PC clock is UK time; after the US close, the 23:00 collector and the 23:15 Nightly Scan, which it waits for). Reuses the Nightly Scan's price update + Surge/Pattern scan (does them itself only if that job didn't succeed), then scores every liquid US stock (price >= $5, >= $10M/day): technical (Trend Template x3, RS Rating/leader, 30-day/52-wk breakout, up-day volume, the 9 pattern scanners capped at 15, Surge signals, minus extension) + smart money (insider open-market buys 30 d, Congress buys filed 45 d AND traded within 120 d, new 13D 30 d, minus heavy insider selling / Form 144). Picks 10 (7 best overall + 3 best smart-money names above MA200), adds Google News RSS (7 d; Yahoo's news API was down), Yahoo fundamentals / analyst target / next earnings, Volume Profile levels, a matplotlib chart (candles, MA21/50/150/200, 52-wk high, S/R, insider/Congress/144/13D markers, volume, RS line) and a rule-based analyst note. Every ticker in the report (summary, stock notes, smart-money monitor, track record) carries the dashboard's 🏛 / 💼▲n·▼n badges (`load_activity()` / `_badges()`, counts frozen at report time: insider P/S 180 d, Congress 12 months up to as_of); clicking opens a popup fed live by `/api/insider/ticker/<T>` / `/api/congress?ticker=` (works when the report is opened through the dashboard). Writes `reports/daily/<as_of>.html` (git-ignored) and `daily_report_picks` (repeat appearances + track record vs S&P 500). Served at `/report` (latest), `/report/<date>`, `/reports`; header button "Morning Brief". CLI: `python daily_brief.py [--as-of DATE] [--no-update] [--force]` |
| `intraday_monitor.py` | **Hourly intraday monitor** (daemon thread started by `api_server.py`, US hours: first pass 10:00 ET, every 60 min, plus a 16:20 ET post-close pass; the 30-min Surge monitor is separate and unchanged). (1) **Focus list** = Monitor watchlists ∪ latest Morning Brief picks ∪ Holdings — 1y daily bars per ticker (`fetch_bars`): Monitor target-range hit (shared `apply_monitor_ranges()`, also used by `POST /api/monitor/refresh`), brief pick below its stop / above first resistance (`daily_report_picks.stop_level/resistance_level`), below MA21 (was above yesterday), new 30-day / 52-wk high, ±5% move, volume pace ≥2.5x. (2) **Whole market** — Yahoo batch quote endpoint (`/v7/finance/quote` via yfinance's `YfData` cookie/crumb, 250 symbols/request ≈ 5 requests for ~1,100 stocks), liquid names only ($5, $10M/day): 52-wk high breakout on volume (pace ≥1.5x), possible Surge-strategy day forming (pace ≥ `surge_strategy.CONFIG['VOL_MULT']`, above MA200, MA50>MA200), ±5% on volume, MA200 reclaim, Trend-Template leader (≥6/8) losing MA50. Prior-day context from stored data (stocks_daily ∪ universe_prices). Vol pace = volume so far / (Vol MA10 × session fraction) — linear, overstates early. Writes `intraday_alerts` and `live_quotes`. **Intraday bars** (`STORE_INTRADAY_BARS`, `SCAN_PATTERNS_INTRADAY`, default on): after the alerts, every whole-market pass turns the batch quotes (+ the ^GSPC RS benchmark) into today's still-forming daily bar (open / day high / day low / last price / volume so far) and feeds it to `fetch_data.quick_update(bars=...)` — no download; indicators, RS Rating/Rank and Trend Template are recomputed for today exactly as the nightly update does — then `scan_patterns.scan_all_patterns(today)` (~40 s + 7 s per pass). So Signals, Pattern Scanner and Data show the current price during the session (rows dated today carry an amber "intraday" tag until 16:20 ET); the 16:20 post-close pass stores a near-final bar and the Nightly Scan's normal quick update overwrites it with Yahoo's final daily bar. Things that need closed candles already ignore today's bar until 16:20 ET (Surge scan `drop_forming_bars`, `daily_brief.latest_complete_date`, weekly ranking). Skipped while a Fetch / quick update runs (`BAR_STORE_BLOCKER`, set by `api_server.py`), and `/api/fetch` + `/api/us/quick-update` answer 409 while a pass is running. Shown in the Market tab's **Intraday Monitor** panel (My list / All / Whole market, Run now); the page polls `/api/intraday/alerts` every minute → Market tab badge + browser notifications for my-list alerts. CLI (one pass now): `python intraday_monitor.py [--market-only\|--focus-only]` |
| `rotation.py` | **Factor & Sector Rotation** (ETF proxies of a Bloomberg Factor Monitor; page `/rotation`, header button "Rotation"). Everything editable is in `CONFIG` at the top: factor pairs (Low Vol USMV−SPY, Low Vol vs High Beta SPLV−SPHB, Value IWD−IWF, Growth IWF−IWD, Momentum MTUM−SPY, Quality QUAL−SPY, Size IWM−SPY, Dividends SCHD−SPY, Buybacks PKW−SPY, ESG ESGU−SPY), `UNAVAILABLE` (Liquidity / EPS Revisions / Short Interest — listed, never computed), the 11 SPDR sectors vs SPY, windows, flag rules and `REGIME` (rules list checked in order — momentum crash: Momentum z < −2; defensive: Low Vol & Value z > +1.5 with Growth & Momentum z < −1; risk-on: the reverse — plus `ZSCORE_BASIS` '1d'/'5d' and `MIN_DAYS` persistence). Daily spread = long ETF return − short ETF return on dates where BOTH have a close (a day missing for one ETF folds into the next common day); periods/weeks/months/MTD/YTD compound the daily spread. z = today vs the previous 60 days (today excluded); RS-Ratio = 100 × RS ÷ SMA63(RS), RS-Momentum = 100 × RS-Ratio ÷ RS-Ratio 10 days ago, quadrants split at 100 (JdK RRG approximation). Today's bar is dropped until 16:20 ET. Pure functions (`pair_returns`, `compound`, `trailing_zscore`, `rs_lines`, `rrg_quadrant`, `period_aggregate`, `rank_desc`, `classify_regime`, `regime_episodes`, `compute_all`) are tested by `scripts/test_rotation.py`. `refresh()` = incremental (re-downloads the last 15 days; a ticker whose stored closes moved > 0.3% — Yahoo dividend re-adjustment — is fully re-backfilled; failed tickers recorded with their last good date), then `recompute()` rebuilds `rotation_series` + `rotation_regimes` from `rotation_prices` (no download). Run nightly by `nightly_scan.py` (23:15); also the page's "Refresh data" button. Extension points (not built): `CONFIG["EXTENSIONS"]` (13F, FINRA short interest, CFTC COT), `EXTENSION_LOADERS`, table `rotation_ext_series`. CLI: `python rotation.py --backfill \| --refresh \| --recompute \| --show` |
| `templates/rotation.html` | The `/rotation` page (standalone, dashboard styling, Chart.js 4.4.0): regime banner, Daily / Weekly / Monthly toggle (`#weekly` / `#monthly` deep links), factor + sector tables sorted by 1D/5D/1M/3M/MTD/YTD with z, rank + change vs 1 d / 5 d, 10-day heatmap and 5D/20D/60D cumulative, RRG columns + quadrant; cumulative-spread chart (1M/3M/6M/1Y, factors/sectors), sector Relative Rotation Graph with 10-day tails; 24-month / 26-week heatmaps with rank, rank change, ⚑ flags (rank moved ≥ 7 places or sign flip ≥ 3% / 1.5%), MTD/YTD and per-bucket regime; regime history |
| `sec_filings.py` | SEC Form 144 (proposed insider sales) + Schedule 13D/13G (>5% holders) collector for the whole market: EDGAR daily form indexes (`form.YYYYMMDD.idx`) -> each filing's XML (`primary_doc.xml`, fallback full `.txt`), issuer CIK -> ticker via `company_tickers.json`, saved per day. Only the XML-era filings are parsed (Form 144 since 2023, `SCHEDULE 13D/G` since Dec 2024); legacy free-text `SC 13D/G` are counted as skipped. CLI: `python sec_filings.py FROM [TO]` |
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
| `insider_trades_all` | Complete SEC Form 4 store from the nightly collector — every filer (incl. 10% owners / "Other"), every transaction code (`transaction_code` + readable `transaction_type`: Purchase/Sale/Award/Option Exercise/Tax Withholding/Gift/...), any size, plus `acquired_disposed`, `shares_owned_after`, `direct_indirect`, `issuer_cik`; keyed UNIQUE(accession, tx_index). Not shown in the UI yet — for later analysis |
| `sec_form144` | Form 144 notices, one row per filing (UNIQUE accession): seller, relationship, shares_to_sell, market_value, approx_sale_date, broker, how acquired, `plan_10b51` + plan_adoption_date — see `sec_filings.py` |
| `sec_13dg` | Schedule 13D/13G (+/A), one row per reporting person per filing (UNIQUE accession + person_index): filer, shares_owned, percent_of_class, voting/dispositive power, event_date, 13G `rule`, 13D Item 4 `purpose` (truncated 2,000 chars). `ticker` is NULL when the issuer has no SEC ticker (e.g. many ETFs/funds) |
| `daily_report_picks` | The 10 ideas of each Morning Brief (as_of, rank, ticker, score/tech/smart, close, headline, tags, risks, stop_level/support_level/resistance_level — the note's key levels, used by the intraday monitor) — UNIQUE(as_of, ticker) |
| `intraday_alerts` | Intraday monitor alerts, UNIQUE(trade_date, ticker, alert_type); scope `focus`/`market`, price, change_pct, vol_pace, level, detail, first_seen/last_seen (ET) |
| `live_quotes` | Latest hourly-monitor quote per ticker (PK ticker): trade_date (ET), price, prev_close, change_pct, volume, quote_time, market_state — written by every whole-market intraday pass; display only (Data tab Live columns), never mixed into `stocks_daily` / `universe_prices` (closed sessions only) |
| `rotation_prices` | Raw adjusted daily closes (+ volume) of the rotation ETFs, PK (ticker, date) — the only downloaded data; everything below is rebuilt from it |
| `rotation_series` | Computed daily series per factor/sector (PK series_id, date): long_ret, short_ret, spread, zscore (vs prior 60 days), rs_ratio, rs_momentum |
| `rotation_regimes` | Regime episodes (regime, start_date, end_date NULL = current, days, detail = JSON z-scores on the start day) — rebuilt on every recompute |
| `rotation_meta` | key/value: last_refresh, last_recompute, as_of, failed ({ticker: last good date}) |
| `rotation_ext_series` | Extension point for 13F / FINRA short interest / CFTC COT series (source, series_id, date, value) — empty, not used yet |
| `data_collection_runs` | One row per nightly collector job (job, status, JSON detail) |
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
GET  /report  /report/<date>  /reports   Morning Brief (latest / one day / archive) from daily_brief.py

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
PUT    /api/monitor/<id>/range          {range_low?, range_high?} — sets target range, re-arms (clears range_hit)
POST   /api/monitor/<id>/reset-range    re-arms without changing the range (clears range_hit only)
POST   /api/monitor/refresh             {watchlist_id?} — live price refresh (Yahoo) + range-hit check
DELETE /api/monitor/<id>
POST   /api/monitor/upload
GET    /api/monitor/download

# Insider trading
GET    /api/insider?from=&to=
DELETE /api/insider/<id>
POST   /api/insider/scan                {tickers, from_date, to_date?} — runs insider_pipeline
GET    /api/insider/scan/status
GET    /api/insider/tickers?days=180      per-ticker open-market buys/sells summary {T: [buys, buy_value, sells, sell_value, last_date]} — powers the insider badge
GET    /api/insider/ticker/<T>?days=365&all=1   one ticker's Form 4 transactions (badge popup; default P+S only, all=1 adds awards/exercises/tax/gifts)

# Congress / political trading disclosures (House + Senate PTRs)
GET    /api/congress?ticker=&politician=&chamber=&party=&type=&from=&to=  (politician is a case-insensitive substring match)
GET    /api/congress/summary?month=YYYY-MM  or  ?month_from=&month_to=   top BUY/SELL tickers for a month or month range
DELETE /api/congress/<id>
POST   /api/congress/fetch              downloads latest snapshot, upserts, re-flags clusters
GET    /api/congress/fetch/status

# Intraday monitor (hourly) — see intraday_monitor.py
GET  /api/intraday/alerts?date=&scope=focus|market   alerts for a session (default latest) + monitor status
POST /api/intraday/refresh              run one pass now
GET  /api/live-quotes                   latest session's hourly quotes {trade_date, updated_at, quotes: {T: {price, prev_close, change_pct, volume, time, state}}}

# Factor & Sector Rotation (rotation.py) — page at GET /rotation
GET  /api/rotation/summary              latest-day factor + sector rows (all periods, z, ranks, heat, cum, RRG), regime, meta
GET  /api/rotation/series?kind=factor|sector&range=1M|3M|6M|1Y   cumulative spread lines rebased to 0
GET  /api/rotation/calendar?freq=monthly|weekly   24-month / 26-week heatmap with ranks, flags, MTD/YTD, bucket regimes
GET  /api/rotation/regimes              regime history + rules
POST /api/rotation/refresh              incremental download + recompute (409 if one is running)

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

# Weekly up/down counter (tab 14) — live Yahoo 1wk bars, not stored data
GET /api/weekly-stats?ticker=&from=&to=&interval=week|day   summary (up/down/flat counts, up- vs down-week avg/total volume, highest-volume week) + per-week list

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
4. **Monitor** — named watchlists with signal price tracking; per-row `+ Backtest` button; per-row editable
   target price range with an **⚡ Refresh Prices** button (live Yahoo price, see Named Watchlists below) that
   sets a "🎯 Hit" alert the first time the price lands in range, staying set until manually Reset
5. **Pattern Scanner** — 9 pattern scans (incl. VCP, MA Squeeze), single-date or date-range scan, ticker filter, VCP-only filter, per-row `+ Backtest` button
6. **Ticker List** — manages extraction_tickers (what gets fetched)
7. **Backtester** — strategy backtester with selection queue
8. **Insider** — insider Form 4/6-K buy/sell signals, cluster-buy detection, on-demand scan by ticker + date range
9. **Congress** — House + Senate + executive branch (President/VP/Cabinet) stock trade disclosures (PTRs / OGE 278-T), cluster-buy detection, "Fetch Data" downloads the latest snapshot
10. **Market** — Intraday Monitor panel (hourly alerts, see `intraday_monitor.py`) on top, then IBD-style Distribution Day / Follow-Through Day market health (Nasdaq + S&P 500), status banner + 60-day chart
11. **Data** — combined data browser + RS Rating screener: tracked Ticker List *and* the full S&P 500 + Russell 1000 universe in one table (latest snapshot, per-ticker history, Trend Template criteria, RS Rating/Score/Line, "Tracked only" + RS Rating ≥ filters, universe admin card)
12. **Surge Strategy** — workflow board for the Volume Surge strategy: Buy Signals (the price TOUCHED the buy level; "I bought" / Dismiss) (**entry rule = "touch"**: a day triggers only if its range [Low, High] includes the surge-day close AT OR ABOVE that day's MA21 (`REQUIRE_BUY_ABOVE_MA21`, default True — never buy into a stock already trading below its MA21), fills at that price; a stock that stays above/below the level, or only touches it while below MA21, for all 5 days is never bought — configurable via `surge_strategy.CONFIG["ENTRY_RULE"]`, "high" = old High >= level rule; every signal row shows **Price now** — the live monitor's price when it has one, else the latest stored close — and **vs limit / vs level**: amber when the price is already ABOVE the buy level, i.e. a buy order at that level would not fill right now) → **Drop Day** (first red candle found, the surge-day close is the buy level for 5 trading days — no touch in that window = deal closed; a blue 🔻 DROP DAY alert + NEW badge fires for a fresh one; `status='armed'` in the DB) → Watching (surge found, no red candle yet), then My Positions (marked as bought; +15% target, last close vs MA21, P/L, "Half sold"/"Sold"/"Undo") and Closed. A triggered signal goes stale 3 trading days after its trigger day. **Quick US update** (~70 s: last 20 days, batched, then scans) is the fast way to bring the data current — the header **Fetch US Data** is the slow full 2-year refresh; "Scan Now" only reads stored daily data (so it needs one of them first) to find new surge signals; "Live Refresh" (and the automatic 30-min monitor while `api_server.py` runs) updates positions with live prices. A scan (Scan Now, or the scan after a fetch) **ignores today's still-forming daily bar until the close** (16:20 ET / 16:20 HKT) — a fetch made while the market is open stores a partial bar whose red candle can still turn green (`drop_forming_bars()`), so a drop day is only ever taken from a closed candle. The live monitor also watches the "Watching" signals and, once a red candle has CLOSED (after 16:20 ET; never from a still-forming candle), promotes them to Drop Day the same evening without waiting for a Fetch + Scan. An alert banner lists SELL / MA21-warning / +15% / new Buy Signal / new Drop Day items, the tab shows an alert-count badge, and "Enable alerts" turns on browser notifications (page must stay open). Every Buy Signal / Drop Day / Watching row also has a **+ Watch** button (same mechanism as the Pattern Scanner's, POST `/api/monitor`, named-watchlist picker if more than one list exists) to add that ticker to the Monitor tab directly from the signal — shows "✓ Watching" once added.

13. **HK Surge** — the same Volume Surge workflow for Hong Kong stocks, fully separate from tab 12: its own data (`hk/hk_stocks.db`), its own fetch buttons (**Fetch HK Data (quick)** ≈ 40 s for the ~800 stocks passing the size/liquidity screen, **Full refresh** ≈ 5 min for all ~2,800 HKEX equities, **Refresh shares**), its own positions and its own live monitor on Hong Kong hours (09:45-16:20 HKT, lunch skipped, every 30 min). Signals are filtered to market cap >= HK$1B and turnover >= HK$5M/day (both measured the day before the surge). Same Drop Day stage, alert and Price now / vs level columns as the US tab (promoted after 16:20 HKT). Buy Signals show a suggested size from the account model: 20 slots x HK$10,000, profit shared evenly over free slots, one position <= 10% of equity, ~0.32% round-trip costs, rounded down to whole board lots. Positions take an HK$ amount or shares so the account (cash, free slots, next stake, realized P/L) can be tracked. Same **+ Watch** button on every signal row as the US tab.

14. **Weekly / Daily** (tab key still `weekly`) — a **Bars** switch at the top of the tab (`wk-interval`, `wkSetInterval()`; "Weekly + Daily (side by side)" is the default, or Weekly only / Daily only) runs both sections below per week and/or per trading day. Side by side = two parallel requests per section: the single-ticker Metric table gets a Weekly and a Daily column (`_wkMetricCells()`) with the week-by-week and day-by-day tables next to each other below it (`_wkBarsPanel()`), and the ranking shows the two lists next to each other (`_wkRankPanel()`), tickers present in both highlighted with their rank in the other list (`W#n` / `D#n`). Backend: `interval=week|day` on `/api/weekly-stats` and `/api/weekly-stats/ranking`, `interval=` argument on `weekly_up_down()` / `rank_volume_ratio()`, `--daily` on the CLI. Daily mode applies the same candle rule to each day (up day = Close > Open, not close vs previous close), keeps the weekly JSON field names (`weeks`, `up_weeks`, `best_week`, … — each "week" is one day; `interval` in the response says which), does no Monday snap, treats `basis=week` as "best single day vs the previous day's close", and leaves today's bar out of the ranking while the US market is open (`surge_strategy.market_clock()`). The weekly description follows. Weekly up/down counter: ticker + date range (defaults to the last year) → Metric/Value summary (up/down/flat weeks, avg + total volume on up vs down weeks and their ratio, highest-volume week, first→last close) plus a week-by-week table (newest first, highest-volume week highlighted, partial edge weeks marked `*`). Pulls weekly bars live from Yahoo via `weekly_stats.py` — independent of `stocks_daily`, so any Yahoo ticker works. A range starting mid-week gets a first bar trimmed to the start date (Yahoo behaviour); the current week's bar is still forming. Below it, a **ranking** section ("Top tickers by volume ratio (up/down avg)", `GET /api/weekly-stats/ranking?from=&to=&top=30&min_change=10&basis=range|week`, `weekly_stats.rank_volume_ratio()`): top 30 tickers by avg volume on up weeks / down weeks, up side only (ratio > 1), default range the last 4 months, limited to tickers whose price rose more than 10% — `basis=range` (default: last close vs the close just before the range) or `basis=week` (best single week vs the previous week's close). Unlike the single-ticker analysis it reads the STORED daily bars (tracked Ticker List ∪ active universe, tracked wins) rolled up into weeks, so it ranks ~1,100 tickers in about a second but is only as current as the last Fetch; whole weeks only (start snapped to its Monday, the still-forming current week left out). Clicking a ranked ticker runs the single-ticker analysis over the same range.

15. **Rotation** — the `/rotation` Factor & Sector Rotation page (see `rotation.py` / `templates/rotation.html`) embedded in an iframe (`loadRotation()`: loaded on first visit, reloaded on revisit / Refresh; "Open in a new window" link; the page hides its own "← Dashboard" button when embedded). Also reachable from the header button "Rotation".

`TABS` array in `dashboard.html` (`['holdings','signals','sold','monitor','patterns','tickers','backtester','insider','congress','market','dataview','surge','hk','weekly','rotation']`) order must match the HTML tab-btn order. There is no standalone RSI Signals tab — it was merged into Signals.

## Holdings — Cut Off Price column
Calculated per row, shown in red:
- **At a loss** (current price ≤ avg buy): `avg_buy_price × 89%`
- **In profit** (current price > avg buy): `high_30d × 89%`

## Named Watchlists (Monitor tab)
- `watchlists` table holds named lists; `monitor_list.watchlist_id` ties each entry to one (default `id=1`).
- `GET/POST/DELETE /api/watchlists` manage the lists themselves; `/api/monitor?watchlist=<id>` filters entries.
- Deleting a watchlist cascades and deletes its `monitor_list` entries.
- **Target price range + live "Refresh Prices"**: each `monitor_list` row has an editable `range_low`/`range_high`
  (inline number inputs in the "Target Range" column) plus `range_hit`/`range_hit_at`/`last_price`/`last_checked`.
  The **⚡ Refresh Prices** toolbar button (`POST /api/monitor/refresh`, scoped to the currently selected watchlist
  tab, or every ticker if "All" is selected) fetches live prices via `surge_strategy.fetch_live_bars()` (Yahoo,
  ~15 min delayed, `period="5d"` — separate from the Surge Strategy tabs' own 30-min monitors, this is on-demand
  only) and, for every row whose live price lands inside `[range_low, range_high]` for the first time, sets
  `range_hit=1` and records `range_hit_at`. Once hit, a later out-of-range refresh does **not** clear it — the
  "Alert" column shows a "🎯 Hit" pill with a **Reset** button (`POST /api/monitor/<id>/reset-range`) that clears
  `range_hit` while keeping the range values, so the same range can be re-armed. Editing the range values
  themselves (`PUT /api/monitor/<id>/range`) also clears any existing hit (a new range hasn't been checked yet).
  A ticker present in more than one watchlist shares one live price fetch but each row's own range is checked
  independently.

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
- **Insider badge** (💼▲n green = n open-market buys, 💼▼n red = sales only; last 180 days, codes P/S from `insider_trades_all`, `insider_signals` as fallback) next to the 🏛 Congress badge — both via `_actBadges(ticker)` on every US ticker column: Holdings, Signals, Sold, Monitor, Pattern Scanner (both views), Ticker List, Backtester (queue + batch results), Insider, Congress (table + monthly summary), Market (intraday alerts), Data, Surge Strategy (signals + positions), Weekly ranking; not HK Surge (US-only data); click opens a popup with every transaction (period selector, option to include awards/exercises/tax/gifts). Both badge sets are re-read every 30 min (`_refreshActivityBadges()`) so the nightly collector's data appears without a page reload; the Monitor table redraws when they change.
- Form 4 tickers are cleaned by `parser.normalize_ticker()` ('NYSE: KRC' -> KRC, '(SIRI)' -> SIRI, 'LEN, LEN.B' -> LEN, 'vicr' -> VICR; non-US 'ASX:...' kept as is).

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
- **Live / Live % columns**: the latest hourly intraday-monitor quote (`live_quotes`, via `/api/live-quotes`, `_dvLoadLive()`), shown only when its session is newer than the row's stored date — so during the day they show today's price while Close/Day % still show the last closed session (the daily tables are only updated after the close, by the 23:15 Nightly Scan). Re-read when the tab is revisited and automatically after each new hourly pass; a note next to the row count gives the quote time.
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
- Tab switching: `showTab(name)` — `TABS` array (15 items) order must match HTML tab-btn order
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
- **Drop day minimum decline**: `CONFIG["MIN_DROP_PCT"]` (surge_strategy.py, default 0.02 = 2%, the user's chosen threshold) / `MIN_DROP_PCT` module constant (scripts/study_volume_surge_2026_04.py) requires the drop-day red candle's own open->close decline to reach this fraction (e.g. 0.02 = 2%, the current default) or the search keeps going for a later, bigger red candle. 0 = any red candle counts (the original rule). `--min-drop-pct` on hk_study.py / study_entry_modes.py / us_backtest_report.py. Live in the dashboard since 2026-09-22 (US and HK); cuts US touch+MA21-filter trades 149->112 (avg 4.3%->5.2%, win 45%->53%, sim P/L $7,438->$6,732) and HK 681->604 (avg 2.6%->2.2%, win 43%->41%, sim P/L +HK$145k->+HK$102k). study_entry_modes.py / hk_study.py / us_backtest_report.py --min-drop-pct now defaults to the live value (study.MIN_DROP_PCT), not a hardcoded 0 -- pass --min-drop-pct 0 explicitly to compare against no threshold.
- **Backtest fill realism**: the surge studies (`scripts/study_volume_surge_2026_04.py`, `hk/hk_study.py`, `scripts/us_backtest_report.py`) default to `ENTRY_MODE="touch"` — the SAME rule the dashboard uses: within 5 trading days of the drop day a day counts only if its range [Low, High] touches the surge-day close, filled at that price; no touch = deal closed. Alternatives kept for comparison: `"optimistic"` (the original: fill at the level whenever High >= level, even when the stock never traded there — overstates results ~3x, 35-40% of trades), `"stop"` (High >= level, pay max(open, level)), `"limit"` (Low <= level, pay min(open, level)). `scripts/study_entry_modes.py` runs the US study in all of them, optionally over several volume multiples with `--vol-mults` (output `us_entry_runs/vm<X>/<mode>/`, git-ignored); `hk_study.py --entry-mode` does HK. `scripts/verify_surge_engine.py` checks the live engine finds exactly the same entries as the study code. Quote touch/stop/limit numbers, not optimistic ones.
- **10-year US surge backtest**: `scripts/build_us_10y_prices.py` downloads daily bars from 2015-09 for the US universe (tracked + active S&P 500 / Russell 1000, `.HK` etc. dropped) into a SEPARATE database `us_backtest_10y/prices_10y.db` (git-ignored; `stock_dashboard.db` is only read) with the Vol MA10 / MA50 / MA200 the study needs. `scripts/us_backtest_report.py --db us_backtest_10y/prices_10y.db --from 2016-10 --to 2026-09 --data-through <last full day>` then runs the unchanged study code on it. `--exclude-acquisitions` (`scripts/acquisition_filter.py`) drops signals whose surge day has an SEC merger / tender-offer filing (PREM14A/DEFM14A/PREM14C/DEFM14C, SC TO-T/TO-C, SC 14D9/14D9C, SC 13E3, 425) from 5 days before to 75 days after it, listed on an `Excluded (M&A)` sheet; filings are cached in `us_backtest_10y/sec_merger_filings.json`, SEC User-Agent reused from the Insider pipeline config. Form 425 also catches the BUYER in a deal and SPAC mergers, so the filter is broader than "takeover target". Survivorship: the universe is today's index membership, so most companies that actually were acquired are not in the data at all. First run (2016-10..2026-09, touch, 3.5x, 2% drop, 20 slots x $1,000): 745 signals, +$21,092, win 40.4%, avg trade 2.08%, median -1.58%; with the filter 698 signals, +$21,436, win 40.8%.
- The `Football Video/` directory is an unrelated project that happens to live under this path; ignore it for dashboard work.
