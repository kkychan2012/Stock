# Stock Dashboard

A local web dashboard for tracking stock holdings, monitoring watchlists, and analysing breakout signals using Yahoo Finance data.

## Features

- **Holdings** — track positions with live P/L, moving averages, and 30-day high/low
- **Signals** — scan for Price > MA200 and MA10 > MA30 breakouts across any date range
- **Sold** — record closed positions and track realised P/L
- **Monitor** — watchlist with signal context, price tracking, and free-text comments
- **Ticker List** — manage the universe of stocks to fetch data for
- **Sell from Holdings** — enter sell qty/price to move a position to Sold in one step
- **Fetch Data** — pull historical OHLCV + indicators from Yahoo Finance into a local SQLite database
- CSV / XLSX upload and download on every tab
- Consistent YYYY-MM-DD date handling across all inputs, uploads, and display

## Requirements

- Python 3.10+
- See `requirements.txt` for dependencies

## Installation

```bash
git clone https://github.com/kkychan2012/Stock.git
cd Stock
pip install -r requirements.txt
```

## Usage

```bash
python api_server.py
```

Then open **http://127.0.0.1:5000** in your browser.

Optional flags:

```bash
python api_server.py --port 5001 --host 0.0.0.0 --debug
```

## Project Structure

```
api_server.py        # Flask API + dashboard routes
db_setup.py          # SQLite schema, migrations, date normalisation
fetch_data.py        # yfinance fetcher + indicator calculations
templates/
  dashboard.html     # Single-page frontend
requirements.txt
```

## Database

Data is stored in `stock_dashboard.db` (SQLite, created automatically on first run). The file is excluded from version control — only the schema and code are tracked.

## Indicators Calculated

| Indicator | Description |
|---|---|
| MA6 / MA10 / MA30 / MA50 / MA200 | Simple moving averages |
| High 30D / Low 30D | Rolling 30-day high and low |
| Vol MA10 | 10-day volume moving average |
| Pct Change | Daily % change |
