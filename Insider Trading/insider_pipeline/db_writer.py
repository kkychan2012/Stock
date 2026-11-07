"""
Writes insider pipeline results into the Stock Dashboard SQLite database
(stock_dashboard.db) so they can be viewed in the Insider tab.
"""
import os
import sqlite3
import logging
import pandas as pd

logger = logging.getLogger(__name__)

# Navigate from insider_pipeline/ up two levels to the project root
_DB_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'stock_dashboard.db')
)

_COLS = [
    'filed_date', 'transaction_date', 'ticker', 'company_name',
    'insider_name', 'role', 'transaction_type', 'shares', 'price',
    'total_value', 'cluster_buy', 'flag_10b51', 'filing_url', 'source',
]


def write_to_dashboard(df: pd.DataFrame) -> int:
    """
    Upsert insider signal records into stock_dashboard.db.
    Skips duplicates (same ticker + transaction_date + insider_name + shares).
    Returns the count of newly inserted rows.
    """
    if df is None or df.empty:
        return 0

    if not os.path.exists(_DB_PATH):
        logger.warning("Dashboard DB not found at %s — skipping DB write", _DB_PATH)
        return 0

    # Ensure all expected columns exist (fill missing with None)
    for col in _COLS:
        if col not in df.columns:
            df[col] = None

    conn = sqlite3.connect(_DB_PATH)
    inserted = 0
    try:
        placeholders = ','.join(['?'] * len(_COLS))
        sql = (
            f"INSERT OR IGNORE INTO insider_signals ({','.join(_COLS)}) "
            f"VALUES ({placeholders})"
        )
        for _, row in df[_COLS].iterrows():
            cur = conn.execute(sql, [_coerce(row[c]) for c in _COLS])
            inserted += cur.rowcount
        conn.commit()
    finally:
        conn.close()

    logger.info("DB write: %d new insider records saved to dashboard DB", inserted)
    return inserted


_ALL_COLS = [
    'accession', 'tx_index', 'filed_date', 'transaction_date', 'ticker',
    'company_name', 'issuer_cik', 'insider_name', 'role', 'transaction_code',
    'transaction_type', 'acquired_disposed', 'shares', 'price', 'total_value',
    'shares_owned_after', 'direct_indirect', 'flag_10b51', 'filing_url', 'source',
]


def write_all_trades(records: list) -> int:
    """
    Store EVERY parsed Form 4 transaction (no role/value/code filter) in
    insider_trades_all — the complete raw store, keyed by accession + the
    transaction's position in the filing. Table is created by the project's
    db_setup.setup_database(). Returns the count of newly inserted rows.
    """
    rows = [r for r in records or [] if r.get('accession') and r.get('tx_index') is not None]
    if not rows:
        return 0
    if not os.path.exists(_DB_PATH):
        logger.warning("Dashboard DB not found at %s — skipping full-store write", _DB_PATH)
        return 0

    import sys
    root = os.path.dirname(_DB_PATH)
    if root not in sys.path:
        sys.path.insert(0, root)
    from db_setup import setup_database   # project root module
    setup_database()

    conn = sqlite3.connect(_DB_PATH, timeout=60)
    inserted = 0
    try:
        sql = (
            f"INSERT OR IGNORE INTO insider_trades_all ({','.join(_ALL_COLS)}) "
            f"VALUES ({','.join(['?'] * len(_ALL_COLS))})"
        )
        for r in rows:
            vals = [_coerce(r.get(c)) for c in _ALL_COLS]
            vals[_ALL_COLS.index('flag_10b51')] = 1 if r.get('flag_10b51') else 0
            inserted += conn.execute(sql, vals).rowcount
        conn.commit()
    finally:
        conn.close()
    logger.info("Full store: %d new rows in insider_trades_all (%d parsed)", inserted, len(rows))
    return inserted


def _coerce(val):
    """Convert pandas/numpy types to plain Python for sqlite3."""
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(val, 'strftime'):   # Timestamp / datetime → YYYY-MM-DD string
        return val.strftime('%Y-%m-%d')
    if hasattr(val, 'item'):       # numpy scalar
        return val.item()
    return val
