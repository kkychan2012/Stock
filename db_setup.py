import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "stock_dashboard.db")

# ---------------------------------------------------------------------------
# Date normalisation (kept in sync with api_server.py)
# ---------------------------------------------------------------------------

_DATE_FORMATS = [
    "%Y-%m-%d",
    "%d/%m/%Y", "%d/%m/%y",
    "%m/%d/%Y", "%m/%d/%y",
    "%d-%m-%Y", "%d-%m-%y",
    "%Y/%m/%d",
    "%d.%m.%Y", "%d.%m.%y",
    "%d %b %Y", "%d %B %Y",
    "%b %d, %Y", "%B %d, %Y",
]


def _normalise_date(s):
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def setup_database():
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS stocks_daily (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker      TEXT    NOT NULL,
                date        TEXT    NOT NULL,
                open        REAL,
                high        REAL,
                low         REAL,
                close       REAL,
                volume      INTEGER,
                ma6         REAL,
                ma10        REAL,
                ma30        REAL,
                ma50        REAL,
                ma200       REAL,
                high_30d    REAL,
                low_30d     REAL,
                vol_ma10    REAL,
                price_change REAL,
                pct_change  REAL,
                direction   TEXT,
                fetched_at  TEXT,
                UNIQUE(ticker, date)
            );

            CREATE TABLE IF NOT EXISTS holdings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT    NOT NULL UNIQUE,
                stock_name      TEXT,
                avg_buy_price   REAL,
                qty             INTEGER,
                buy_date        TEXT,
                notes           TEXT,
                added_at        TEXT    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sold (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT    NOT NULL,
                stock_name      TEXT,
                avg_buy_price   REAL,
                qty             INTEGER,
                buy_date        TEXT,
                sell_price      REAL,
                sell_date       TEXT,
                pl_value        REAL,
                notes           TEXT,
                created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS breakout_signals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT    NOT NULL,
                signal_type     TEXT    NOT NULL,
                signal_date     TEXT    NOT NULL,
                close_price     REAL,
                indicator_value REAL,
                created_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ticker, signal_type, signal_date)
            );

            CREATE TABLE IF NOT EXISTS monitor_list (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker       TEXT    NOT NULL UNIQUE,
                stock_name   TEXT,
                reason       TEXT,
                comment      TEXT,
                signal_date  TEXT,
                signal_price REAL,
                added_at     TEXT    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS skipped_stocks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker      TEXT    NOT NULL,
                reason      TEXT,
                skipped_at  TEXT    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS extraction_tickers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker     TEXT    NOT NULL UNIQUE,
                notes      TEXT,
                added_at   TEXT    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pattern_scan_results (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_date    TEXT    NOT NULL,
                ticker       TEXT    NOT NULL,
                pattern_name TEXT    NOT NULL,
                signal_detail TEXT,
                signal_date  TEXT,
                close        REAL,
                ma10         REAL,
                ma30         REAL,
                ma50         REAL,
                ma200        REAL,
                volume       INTEGER,
                vol_ma10     REAL,
                high_30d     REAL,
                low_30d      REAL,
                pct_change   REAL,
                scanned_at   TEXT    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(scan_date, ticker, pattern_name)
            );

            CREATE TABLE IF NOT EXISTS insider_signals (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                filed_date       TEXT NOT NULL,
                transaction_date TEXT NOT NULL,
                ticker           TEXT NOT NULL,
                company_name     TEXT,
                insider_name     TEXT,
                role             TEXT,
                transaction_type TEXT DEFAULT 'Purchase',
                shares           REAL,
                price            REAL,
                total_value      REAL,
                cluster_buy      INTEGER DEFAULT 0,
                flag_10b51       INTEGER DEFAULT 0,
                filing_url       TEXT,
                source           TEXT DEFAULT 'Form 4',
                scan_run_at      TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ticker, transaction_date, insider_name, shares)
            );

            CREATE INDEX IF NOT EXISTS idx_stocks_daily_ticker_date
                ON stocks_daily(ticker, date);

            CREATE INDEX IF NOT EXISTS idx_breakout_signals_date
                ON breakout_signals(signal_date);

            CREATE INDEX IF NOT EXISTS idx_pattern_scan_date
                ON pattern_scan_results(scan_date);

            CREATE INDEX IF NOT EXISTS idx_insider_signals_date
                ON insider_signals(transaction_date DESC);
        """)
        _migrate(conn)
    print(f"Database ready at: {DB_PATH}")


def _fix_existing_dates(conn):
    """Normalise any non-YYYY-MM-DD dates already stored in the database."""
    targets = [
        ("holdings",    ["buy_date"]),
        ("sold",        ["buy_date", "sell_date"]),
        ("monitor_list",["signal_date"]),
    ]
    fixed = 0
    for table, cols in targets:
        # Skip if table doesn't exist yet
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        for col in cols:
            col_exists = any(
                row[1] == col
                for row in conn.execute(f"PRAGMA table_info({table})")
            )
            if not col_exists:
                continue
            rows = conn.execute(
                f"SELECT id, {col} FROM {table} "
                f"WHERE {col} IS NOT NULL AND {col} NOT LIKE '____-__-__'"
            ).fetchall()
            for row in rows:
                normalised = _normalise_date(row[1])
                conn.execute(
                    f"UPDATE {table} SET {col} = ? WHERE id = ?",
                    (normalised, row[0])
                )
                fixed += 1
    if fixed:
        print(f"  Date fix: normalised {fixed} date value(s) to YYYY-MM-DD.")


def _migrate(conn):
    """Add columns / tables introduced after initial release without dropping existing data."""
    existing_cols = {
        "holdings": {row[1] for row in conn.execute("PRAGMA table_info(holdings)")},
        "sold":     {row[1] for row in conn.execute("PRAGMA table_info(sold)")},
    }
    if "buy_date" not in existing_cols["holdings"]:
        conn.execute("ALTER TABLE holdings ADD COLUMN buy_date TEXT")
    if "buy_date" not in existing_cols["sold"]:
        conn.execute("ALTER TABLE sold ADD COLUMN buy_date TEXT")

    monitor_cols = {row[1] for row in conn.execute("PRAGMA table_info(monitor_list)")}
    if "signal_date" not in monitor_cols:
        conn.execute("ALTER TABLE monitor_list ADD COLUMN signal_date TEXT")
    if "signal_price" not in monitor_cols:
        conn.execute("ALTER TABLE monitor_list ADD COLUMN signal_price REAL")
    if "comment" not in monitor_cols:
        conn.execute("ALTER TABLE monitor_list ADD COLUMN comment TEXT")

    existing_tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "extraction_tickers" not in existing_tables:
        conn.execute("""
            CREATE TABLE extraction_tickers (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker   TEXT    NOT NULL UNIQUE,
                notes    TEXT,
                added_at TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)

    if "pattern_scan_results" not in existing_tables:
        conn.execute("""
            CREATE TABLE pattern_scan_results (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_date    TEXT    NOT NULL,
                ticker       TEXT    NOT NULL,
                pattern_name TEXT    NOT NULL,
                signal_detail TEXT,
                signal_date  TEXT,
                close        REAL,
                ma10         REAL,
                ma30         REAL,
                ma50         REAL,
                ma200        REAL,
                volume       INTEGER,
                vol_ma10     REAL,
                high_30d     REAL,
                low_30d      REAL,
                pct_change   REAL,
                scanned_at   TEXT    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(scan_date, ticker, pattern_name)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pattern_scan_date "
            "ON pattern_scan_results(scan_date)"
        )

    if "insider_signals" not in existing_tables:
        conn.execute("""
            CREATE TABLE insider_signals (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                filed_date       TEXT NOT NULL,
                transaction_date TEXT NOT NULL,
                ticker           TEXT NOT NULL,
                company_name     TEXT,
                insider_name     TEXT,
                role             TEXT,
                transaction_type TEXT DEFAULT 'Purchase',
                shares           REAL,
                price            REAL,
                total_value      REAL,
                cluster_buy      INTEGER DEFAULT 0,
                flag_10b51       INTEGER DEFAULT 0,
                filing_url       TEXT,
                source           TEXT DEFAULT 'Form 4',
                scan_run_at      TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ticker, transaction_date, insider_name, shares)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_insider_signals_date "
            "ON insider_signals(transaction_date DESC)"
        )

    _fix_existing_dates(conn)


if __name__ == "__main__":
    setup_database()
