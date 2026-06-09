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


def _base_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Apply role, value, and date filters common to both purchases and sales."""
    if df.empty:
        return df
    df = df[df["role"].apply(_role_matches)]
    if df.empty:
        return df
    df = df[df["total_value"] >= MIN_TRANSACTION_VALUE]
    df = df.dropna(subset=["transaction_date", "total_value"])
    df = df[df["total_value"] > 0]
    df["filed_date"] = pd.to_datetime(df["filed_date"], errors="coerce")
    df["transaction_date"] = pd.to_datetime(df["transaction_date"], errors="coerce")
    df = df.dropna(subset=["filed_date"])
    return df


def apply_filters(records: list[dict]) -> pd.DataFrame:
    """
    Filter raw records to high-conviction purchases and significant sales.
    Returns a DataFrame of qualifying transactions sorted newest first.
    """
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    before = len(df)

    purchases = _base_filter(df[df["transaction_type"] == "Purchase"].copy())
    sales     = _base_filter(df[df["transaction_type"] == "Sale"].copy())

    result = pd.concat([purchases, sales], ignore_index=True)
    after = len(result)
    logger.info("Filter: %d -> %d records after applying criteria", before, after)

    if result.empty:
        return result

    # Re-coerce after concat (empty frames can revert dtypes to object)
    result["filed_date"] = pd.to_datetime(result["filed_date"], errors="coerce")
    result["transaction_date"] = pd.to_datetime(result["transaction_date"], errors="coerce")

    result = result.sort_values("filed_date", ascending=False).reset_index(drop=True)
    return result
