"""
Detect cluster buys: 3+ insiders at the same company buying within 7 days.
"""
import pandas as pd
from config import CLUSTER_WINDOW_DAYS, CLUSTER_MIN_INSIDERS


def detect_clusters(df: pd.DataFrame) -> pd.DataFrame:
    """
    Adds a 'cluster_buy' boolean column.
    A cluster is CLUSTER_MIN_INSIDERS+ distinct insiders at the same ticker
    whose transaction_date falls within a CLUSTER_WINDOW_DAYS rolling window.
    """
    if df.empty:
        return df

    df = df.copy()
    df["cluster_buy"] = False

    # Work per company; use company_name as fallback when ticker is absent
    group_key = df["ticker"].where(df["ticker"].notna(), df["company_name"])
    for key, group in df.groupby(group_key):
        if len(group) < CLUSTER_MIN_INSIDERS:
            continue

        dates = group["transaction_date"].dropna().sort_values()
        if len(dates) < CLUSTER_MIN_INSIDERS:
            continue

        window = pd.Timedelta(days=CLUSTER_WINDOW_DAYS)
        cluster_indices: set = set()

        for i, (idx, date) in enumerate(dates.items()):
            # All transactions within window starting at this date
            in_window = dates[(dates >= date) & (dates <= date + window)]
            # Count distinct insiders
            distinct_insiders = df.loc[in_window.index, "insider_name"].nunique()
            if distinct_insiders >= CLUSTER_MIN_INSIDERS:
                cluster_indices.update(in_window.index.tolist())

        df.loc[list(cluster_indices), "cluster_buy"] = True

    return df
