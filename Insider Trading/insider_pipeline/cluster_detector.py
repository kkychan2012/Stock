"""
Detect cluster buys and cluster sells: 3+ insiders at the same company
transacting in the same direction within CLUSTER_WINDOW_DAYS days.
"""
import pandas as pd
from config import CLUSTER_WINDOW_DAYS, CLUSTER_MIN_INSIDERS


def _mark_clusters(df: pd.DataFrame, subset: pd.DataFrame) -> set:
    """Return index positions that belong to a cluster within subset."""
    cluster_indices: set = set()
    if len(subset) < CLUSTER_MIN_INSIDERS:
        return cluster_indices

    dates = subset["transaction_date"].dropna().sort_values()
    if len(dates) < CLUSTER_MIN_INSIDERS:
        return cluster_indices

    window = pd.Timedelta(days=CLUSTER_WINDOW_DAYS)
    for idx, date in dates.items():
        in_window = dates[(dates >= date) & (dates <= date + window)]
        distinct_insiders = subset.loc[in_window.index, "insider_name"].nunique()
        if distinct_insiders >= CLUSTER_MIN_INSIDERS:
            cluster_indices.update(in_window.index.tolist())

    return cluster_indices


def detect_clusters(df: pd.DataFrame) -> pd.DataFrame:
    """
    Adds a 'cluster_buy' boolean column.
    Marks rows where CLUSTER_MIN_INSIDERS+ distinct insiders at the same ticker
    transact in the same direction (all buys or all sells) within CLUSTER_WINDOW_DAYS.
    """
    if df.empty:
        return df

    df = df.copy()
    df["cluster_buy"] = False

    group_key = df["ticker"].where(df["ticker"].notna(), df["company_name"])
    for key, group in df.groupby(group_key):
        for tx_type in ("Purchase", "Sale"):
            subset = group[group["transaction_type"] == tx_type]
            cluster_idx = _mark_clusters(df, subset)
            if cluster_idx:
                df.loc[list(cluster_idx), "cluster_buy"] = True

    return df
