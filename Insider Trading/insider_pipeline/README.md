# Insider Trading Pipeline

Automated daily scanner for SEC EDGAR Form 4 filings.  
Filters for high-conviction insider buys and outputs a formatted Excel report.

## Setup

```bash
cd insider_pipeline
pip install -r requirements.txt
```

Edit `config.py` and set `SEC_USER_AGENT` to your name and email  
(required by the SEC for all EDGAR API access).

## Usage

### Quick test — last 7 days
```bash
python main.py --days 7
```

### Full 6-month scan
```bash
python main.py
```

### Custom date range
```bash
python main.py --from 2026-01-01 --to 2026-06-08
```

### Single stock or watchlist (fast — seconds instead of hours)
```bash
python main.py --ticker AAPL
python main.py --ticker AAPL,MSFT,NVDA --days 30
```

### Launch the GUI
```bash
python main.py --gui
# or directly:
python gui.py
```

### Daily auto-update (runs at 18:00 every day)
```bash
python main.py --schedule
```

## Output

`insider_trading_report.xlsx` with four sheets:

| Sheet | Content |
|-------|---------|
| Dashboard | Summary stats + top 10 companies by buy value |
| All Buys | All qualifying transactions, newest first |
| Cluster Buys | Transactions where 3+ insiders bought within 7 days |
| Monthly Summary | Pivot by month with counts and totals |

### Row colours
- **Red** — Cluster buy (3+ insiders, same company, ≤7 days)
- **Orange** — Large buy (>$1 million)
- **White** — Standard qualifying buy

## Filtering Criteria

Only transactions that meet **all** of:
- Transaction code = `P` (open market purchase)
- Insider role = CEO / CFO / COO / President / Chairman / Director
- Transaction value ≥ $100,000
- Filed within the configured lookback window

10b5-1 pre-scheduled plans are flagged with `YES` in the `10b5-1 Flag` column  
but are **not** excluded — review them manually.

## Files

```
insider_pipeline/
├── main.py              # Entry point (CLI + GUI launcher)
├── gui.py               # tkinter GUI
├── sec_fetcher.py       # SEC EDGAR HTTP calls + rate limiting
├── parser.py            # Form 4 XML parsing
├── filter.py            # High-conviction filter logic
├── cluster_detector.py  # Cluster buy detection
├── excel_exporter.py    # Excel report generation
├── config.py            # All settings
├── requirements.txt     # Python dependencies
└── README.md            # This file
```

## Performance

The pipeline uses 8 concurrent download workers, rate-limited to 10 req/s.

| Scope | Approx. time |
|-------|-------------|
| 1-day incremental | ~1–2 minutes |
| 7-day scan | ~60–90 minutes |
| 30-day scan | ~4–6 hours |
| 180-day initial scan | ~20–24 hours (run overnight) |

Run `python main.py --days 7` first to verify things work, then run  
`python main.py` for the full 180-day history.

## SEC Rate Limits

All API calls respect SEC EDGAR's 10 req/s limit automatically.  
All errors are retried up to 3 times with exponential backoff.  
Unrecoverable errors are logged to `pipeline.log` and skipped.
