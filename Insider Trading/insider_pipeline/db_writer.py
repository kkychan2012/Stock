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
