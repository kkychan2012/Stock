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
    # Without a busy timeout, a second connection attempting to write while
    # another connection's transaction is still uncommitted fails instantly
    # with "database is locked" instead of waiting — this app opens nested
    # connections in a few places (e.g. rs_calculator.fetch_universe_prices
    # called from within fetch_data.py's own open transaction).
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def setup_database():
    dropped_signals = False
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

            CREATE TABLE IF NOT EXISTS watchlists (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL UNIQUE,
                created_at TEXT    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS monitor_list (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                watchlist_id INTEGER NOT NULL DEFAULT 1,
                ticker       TEXT    NOT NULL,
                stock_name   TEXT,
                reason       TEXT,
                comment      TEXT,
                signal_date  TEXT,
                signal_price REAL,
                added_at     TEXT    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(watchlist_id, ticker)
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
                ma9          REAL,
                ma21         REAL,
                ma100        REAL,
                volume       INTEGER,
                vol_ma10     REAL,
                high_30d     REAL,
                low_30d      REAL,
                pct_change   REAL,
                squeeze_spread_pct REAL,
                squeeze_days       INTEGER,
                squeeze_fresh      INTEGER,
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

            CREATE INDEX IF NOT EXISTS idx_pattern_scan_date
                ON pattern_scan_results(scan_date);

            CREATE TABLE IF NOT EXISTS vcp_signals (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_date              TEXT    NOT NULL,
                ticker                 TEXT    NOT NULL,
                vcp_detected           INTEGER DEFAULT 0,
                num_contractions       INTEGER,
                contraction_depths     TEXT,
                volume_dryup           INTEGER DEFAULT 0,
                pivot_price            REAL,
                suggested_stop         REAL,
                current_price_vs_pivot REAL,
                tightness_pct          REAL,
                close                  REAL,
                scanned_at             TEXT    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(scan_date, ticker)
            );

            CREATE INDEX IF NOT EXISTS idx_insider_signals_date
                ON insider_signals(transaction_date DESC);

            CREATE INDEX IF NOT EXISTS idx_vcp_signals_date
                ON vcp_signals(scan_date);

            CREATE TABLE IF NOT EXISTS ticker_universe (
                ticker      TEXT PRIMARY KEY,
                source      TEXT,              -- 'sp500' / 'russell1000' / 'both'
                added_date  TEXT,
                is_active   INTEGER DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_ticker_universe_active
                ON ticker_universe(is_active);

            CREATE TABLE IF NOT EXISTS universe_prices (
                ticker      TEXT NOT NULL,
                date        TEXT NOT NULL,
                close       REAL,
                fetched_at  TEXT,
                PRIMARY KEY (ticker, date)
            );

            CREATE INDEX IF NOT EXISTS idx_universe_prices_date
                ON universe_prices(date);

            CREATE TABLE IF NOT EXISTS rs_ratings (
                ticker         TEXT NOT NULL,
                date           TEXT NOT NULL,
                rs_score       REAL,
                rs_rating      INTEGER,        -- 1-99
                rs_line_value  REAL,
                rs_line_trend  TEXT,           -- 'up' / 'down'
                rs_leader      INTEGER,        -- 1 when rs_rating>=70 AND rs_line_trend='up'
                created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (ticker, date)
            );

            CREATE INDEX IF NOT EXISTS idx_rs_ratings_date_rating
                ON rs_ratings(date, rs_rating);

            CREATE TABLE IF NOT EXISTS universe_meta (
                id                 INTEGER PRIMARY KEY CHECK (id = 1),
                last_refresh_date  TEXT,
                sp500_count        INTEGER,
                russell1000_count  INTEGER,
                overlap_count      INTEGER,
                total_active       INTEGER
            );

            -- Volume Surge strategy (Scenario A) workflow — see surge_strategy.py.
            -- status: watching -> armed -> triggered -> bought   (auto: watching/armed/
            -- triggered/expired; user-set: bought/dismissed)
            CREATE TABLE IF NOT EXISTS surge_signals (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker            TEXT NOT NULL,
                surge_date        TEXT NOT NULL,
                surge_close       REAL NOT NULL,
                surge_vol_ratio   REAL,
                status            TEXT NOT NULL,
                drop_date         TEXT,
                buy_level         REAL,            -- limit-buy price (= surge_close)
                window_days_left  INTEGER,         -- trading days left in the 5-day buy window
                trigger_date      TEXT,            -- first day High reached buy_level
                note              TEXT,
                first_seen        TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at        TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ticker, surge_date)
            );

            CREATE INDEX IF NOT EXISTS idx_surge_signals_status
                ON surge_signals(status);

            -- Positions the user has marked as actually bought (phase 2+).
            CREATE TABLE IF NOT EXISTS surge_positions (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id          INTEGER UNIQUE REFERENCES surge_signals(id) ON DELETE CASCADE,
                ticker             TEXT NOT NULL,
                surge_date         TEXT NOT NULL,
                surge_close        REAL NOT NULL,
                buy_date           TEXT NOT NULL,
                buy_price          REAL NOT NULL,
                shares             REAL,
                partial_target     REAL,           -- surge_close * (1 + PARTIAL_TARGET_PCT)
                partial_date       TEXT,
                partial_price      REAL,
                status             TEXT NOT NULL DEFAULT 'holding',  -- holding / partial_sold / closed
                exit_date          TEXT,
                exit_price         REAL,
                exit_reason        TEXT,
                last_price         REAL,
                last_ma21          REAL,
                last_checked       TEXT,
                alert_state        TEXT DEFAULT 'ok',  -- ok / below_ma21 / sell
                ma21_break_date    TEXT,           -- day whose close broke MA21 (sell = next open confirmed)
                alert_note         TEXT,
                target_hit_date    TEXT,           -- first day close (or intraday) reached partial_target
                price_asof         TEXT,           -- bar date last_price belongs to
                price_is_live      INTEGER DEFAULT 0,  -- 1 = today's still-forming bar
                note               TEXT,
                created_at         TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        dropped_signals = _migrate(conn)
    if dropped_signals:
        with get_connection() as conn:
            conn.execute("VACUUM")
        print("  Reclaimed disk space from dropped breakout_signals table.")
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
    """Add columns / tables introduced after initial release without dropping existing data.
    Returns True if breakout_signals was dropped this run (caller should VACUUM)."""
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

    # breakout_signals is unused (signals are computed live from stocks_daily) — drop it
    dropped_signals = False
    if "breakout_signals" in existing_tables:
        conn.execute("DROP TABLE breakout_signals")
        existing_tables.discard("breakout_signals")
        dropped_signals = True
        print("  Migration: dropped unused breakout_signals table.")

    # Add watchlists table and migrate monitor_list to support multiple watchlists
    if "watchlists" not in existing_tables:
        conn.execute("""
            CREATE TABLE watchlists (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL UNIQUE,
                created_at TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("INSERT OR IGNORE INTO watchlists (id, name) VALUES (1, 'Default')")

    if "watchlist_id" not in monitor_cols:
        # Recreate monitor_list with watchlist_id and new UNIQUE(watchlist_id, ticker)
        conn.execute("""
            CREATE TABLE monitor_list_new (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                watchlist_id INTEGER NOT NULL DEFAULT 1,
                ticker       TEXT    NOT NULL,
                stock_name   TEXT,
                reason       TEXT,
                comment      TEXT,
                signal_date  TEXT,
                signal_price REAL,
                added_at     TEXT    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(watchlist_id, ticker)
            )
        """)
        conn.execute("""
            INSERT INTO monitor_list_new
                (id, watchlist_id, ticker, stock_name, reason, comment, signal_date, signal_price, added_at)
            SELECT id, 1, ticker, stock_name, reason, comment, signal_date, signal_price, added_at
            FROM monitor_list
        """)
        conn.execute("DROP TABLE monitor_list")
        conn.execute("ALTER TABLE monitor_list_new RENAME TO monitor_list")
        print("  Migration: monitor_list upgraded to support multiple watchlists.")

    # Ensure Default watchlist always exists
    conn.execute("INSERT OR IGNORE INTO watchlists (id, name) VALUES (1, 'Default')")

    # surge_positions live-monitor columns (added after the table first shipped)
    surge_pos_cols = {row[1] for row in conn.execute("PRAGMA table_info(surge_positions)")}
    for col, typ in (("alert_note", "TEXT"), ("target_hit_date", "TEXT"),
                     ("price_asof", "TEXT"), ("price_is_live", "INTEGER DEFAULT 0")):
        if surge_pos_cols and col not in surge_pos_cols:
            conn.execute(f"ALTER TABLE surge_positions ADD COLUMN {col} {typ}")

    pattern_cols = {row[1] for row in conn.execute("PRAGMA table_info(pattern_scan_results)")}
    if "mom_15d_start" not in pattern_cols:
        conn.execute("ALTER TABLE pattern_scan_results ADD COLUMN mom_15d_start REAL")
    if "mom_15d_gain_pct" not in pattern_cols:
        conn.execute("ALTER TABLE pattern_scan_results ADD COLUMN mom_15d_gain_pct REAL")
    if "ma9" not in pattern_cols:
        conn.execute("ALTER TABLE pattern_scan_results ADD COLUMN ma9 REAL")
    if "ma21" not in pattern_cols:
        conn.execute("ALTER TABLE pattern_scan_results ADD COLUMN ma21 REAL")
    if "ma100" not in pattern_cols:
        conn.execute("ALTER TABLE pattern_scan_results ADD COLUMN ma100 REAL")
    if "squeeze_spread_pct" not in pattern_cols:
        conn.execute("ALTER TABLE pattern_scan_results ADD COLUMN squeeze_spread_pct REAL")
    if "squeeze_days" not in pattern_cols:
        conn.execute("ALTER TABLE pattern_scan_results ADD COLUMN squeeze_days INTEGER")
    if "squeeze_fresh" not in pattern_cols:
        conn.execute("ALTER TABLE pattern_scan_results ADD COLUMN squeeze_fresh INTEGER")

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
                ma9          REAL,
                ma21         REAL,
                ma100        REAL,
                volume       INTEGER,
                vol_ma10     REAL,
                high_30d     REAL,
                low_30d      REAL,
                pct_change   REAL,
                mom_15d_start    REAL,
                mom_15d_gain_pct REAL,
                squeeze_spread_pct REAL,
                squeeze_days       INTEGER,
                squeeze_fresh      INTEGER,
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

    if "congress_trades" not in existing_tables:
        conn.execute("""
            CREATE TABLE congress_trades (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_date           TEXT NOT NULL,
                filed_date        TEXT,
                ticker            TEXT,
                company           TEXT,
                politician_name   TEXT NOT NULL,
                chamber           TEXT,
                party             TEXT,
                state_or_district TEXT,
                role              TEXT,
                type              TEXT,
                amount_range      TEXT,
                source            TEXT,
                filing_url        TEXT,
                cluster_buy       INTEGER DEFAULT 0,
                dedup_key         TEXT UNIQUE,
                fetched_at        TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_congress_trades_date "
            "ON congress_trades(tx_date DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_congress_trades_ticker "
            "ON congress_trades(ticker)"
        )

    stocks_daily_cols = {row[1] for row in conn.execute("PRAGMA table_info(stocks_daily)")}
    if "rsi14" not in stocks_daily_cols:
        try:
            conn.execute("ALTER TABLE stocks_daily ADD COLUMN rsi14 REAL")
        except Exception:
            pass

    # Minervini Trend Template columns
    _tt_cols = {
        "ma100":       "REAL",
        "ma150":       "REAL",
        "high_52wk":   "REAL",
        "low_52wk":    "REAL",
        "low_alltime": "REAL",
        "low_alltime_date": "TEXT",
        "rs_raw":      "REAL",
        "rs_rank":     "INTEGER",
        "c1":          "INTEGER",
        "c2":          "INTEGER",
        "c3":          "INTEGER",
        "c4":          "INTEGER",
        "c5":          "INTEGER",
        "c6":          "INTEGER",
        "c7":          "INTEGER",
        "c8":          "INTEGER",
        "trend_score": "INTEGER",
    }
    for col, typ in _tt_cols.items():
        if col not in stocks_daily_cols:
            conn.execute(f"ALTER TABLE stocks_daily ADD COLUMN {col} {typ}")

    # universe_prices: widen from Close-only to full OHLCV + the same
    # indicator/Trend Template columns stocks_daily has, so MAs/Trend
    # Template can be computed for the full S&P500+Russell1000 universe too.
    universe_prices_cols = {row[1] for row in conn.execute("PRAGMA table_info(universe_prices)")}
    _up_cols = {
        "open":        "REAL",
        "high":        "REAL",
        "low":         "REAL",
        "volume":      "INTEGER",
        "ma6":         "REAL",
        "ma10":        "REAL",
        "ma30":        "REAL",
        "ma50":        "REAL",
        "ma150":       "REAL",
        "ma200":       "REAL",
        "high_30d":    "REAL",
        "low_30d":     "REAL",
        "high_52wk":   "REAL",
        "low_52wk":    "REAL",
        "low_alltime": "REAL",
        "low_alltime_date": "TEXT",
        "vol_ma10":    "REAL",
        "price_change": "REAL",
        "pct_change":  "REAL",
        "direction":   "TEXT",
        "rsi14":       "REAL",
        "rs_raw":      "REAL",
        "rs_rank":     "INTEGER",
        "c1":          "INTEGER",
        "c2":          "INTEGER",
        "c3":          "INTEGER",
        "c4":          "INTEGER",
        "c5":          "INTEGER",
        "c6":          "INTEGER",
        "c7":          "INTEGER",
        "c8":          "INTEGER",
        "trend_score": "INTEGER",
    }
    for col, typ in _up_cols.items():
        if col not in universe_prices_cols:
            conn.execute(f"ALTER TABLE universe_prices ADD COLUMN {col} {typ}")

    # skipped_stocks: keep at most one row per ticker (was growing unbounded — one
    # row per failed/skipped fetch attempt, even repeat attempts on the same day)
    idx_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_skipped_stocks_ticker'"
    ).fetchone()
    if not idx_exists:
        conn.execute("""
            DELETE FROM skipped_stocks
            WHERE id NOT IN (SELECT MAX(id) FROM skipped_stocks GROUP BY ticker)
        """)
        conn.execute(
            "CREATE UNIQUE INDEX idx_skipped_stocks_ticker ON skipped_stocks(ticker)"
        )
        print("  Migration: deduplicated skipped_stocks and enforced one row per ticker.")

    _fix_existing_dates(conn)
    return dropped_signals


if __name__ == "__main__":
    setup_database()
