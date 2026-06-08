"""
Apply high-conviction filtering rules to raw transaction records.
"""
import logging
import pandas as pd
from datetime import datetime

from config import MIN_TRANSACTION_VALUE, ROLES_TO_TRACK

logger = logging.getLogger(__name__)

_ROLE_SET = {r.lower() for r in ROLES_TO_TRACK}


def _role_matches(role: str) -> bool:
    if not role:
        return False
    lower = role.lower()
    return any(r in lower for r in _ROLE_SET)


def apply_filters(records: list[dict]) -> pd.DataFrame:
    """
    Filter raw records to high-conviction buys.
    Returns a DataFrame of qualifying transactions.
    """
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    before = len(df)

    # Only open-market purchases (code=P already enforced in parser, but guard here)
    df = df[df["transaction_type"] == "Purchase"]

    # Role filter
    df = df[df["role"].apply(_role_matches)]

    # Value filter
    df = df[df["total_value"] >= MIN_TRANSACTION_VALUE]

    # Drop rows with missing critical fields
    df = df.dropna(subset=["transaction_date", "total_value"])
    df = df[df["total_value"] > 0]

    # Parse dates
    df["filed_date"] = pd.to_datetime(df["filed_date"], errors="coerce")
    df["transaction_date"] = pd.to_datetime(df["transaction_date"], errors="coerce")
    df = df.dropna(subset=["filed_date"])

    after = len(df)
    logger.info("Filter: %d -> %d records after applying criteria", before, after)

    # Sort newest first
    df = df.sort_values("filed_date", ascending=False).reset_index(drop=True)
    return df
