"""
rotation.py — Factor & Sector Rotation monitor built from free ETF proxies.

Replicates the *idea* of a Bloomberg Factor Monitor (long-short factor returns, sorted by
1D) with ETF pairs: each factor's daily spread = long ETF daily return - short/benchmark ETF
daily return. These are ETF PROXIES, not Bloomberg's (or MSCI/Barra's) true factor returns:
the ETFs are long-only, sector-unconstrained and rebalanced on their own schedules.

Data flow (raw prices are stored separately from everything computed):
  Yahoo adjusted daily closes --(backfill/refresh)--> rotation_prices
  rotation_prices --(recompute, pure functions below)--> rotation_series + rotation_regimes
  last refresh / failed tickers ---------------------> rotation_meta

No look-ahead:
  * a z-score on day t uses only the 60 spreads BEFORE t (today is excluded from the window);
  * the regime on day t uses only that day's z-scores; RS-Ratio uses a trailing window;
  * today's still-forming bar is dropped until the US close is final (16:20 ET).
Missing days: each pair is computed on the dates where BOTH ETFs have a close, so holidays
drop out, and a failed day for one ETF folds into the next day's (multi-day) return instead of
being treated as a zero or dropped.

CLI:
  python rotation.py --backfill     # (re)download ~26 months for every ETF, then recompute
  python rotation.py --refresh      # incremental: last ~15 days (+ auto re-backfill on
                                    #   dividend re-adjustment drift), then recompute
  python rotation.py --recompute    # rebuild spreads/regimes from stored prices, no download
  python rotation.py --show         # print the latest-day table
"""

import argparse
import json
import logging
import threading
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from db_setup import get_connection, setup_database

log = logging.getLogger("rotation")

# ---------------------------------------------------------------------------
# CONFIG — every editable setting lives here.
# ---------------------------------------------------------------------------
CONFIG = {
    "BENCHMARK": "SPY",
    # factor id -> long ETF minus short ETF.  Add/remove freely; the page follows.
    "FACTORS": {
        "low_vol":    {"label": "Low Volatility",       "long": "USMV", "short": "SPY"},
        "low_vol_hb": {"label": "Low Vol vs High Beta", "long": "SPLV", "short": "SPHB"},
        "value":      {"label": "Value",                "long": "IWD",  "short": "IWF"},
        "growth":     {"label": "Growth",               "long": "IWF",  "short": "IWD"},
        "momentum":   {"label": "Momentum",             "long": "MTUM", "short": "SPY"},
        "quality":    {"label": "Quality",              "long": "QUAL", "short": "SPY"},
        "size":       {"label": "Size (small)",         "long": "IWM",  "short": "SPY"},
        "dividends":  {"label": "Dividends",            "long": "SCHD", "short": "SPY"},
        "buybacks":   {"label": "Buybacks",             "long": "PKW",  "short": "SPY"},
        "esg":        {"label": "ESG",                  "long": "ESGU", "short": "SPY"},
    },
    # Bloomberg factors with no free ETF proxy — listed on the page, never computed.
    "UNAVAILABLE": {
        "liquidity":      "Liquidity",
        "revisions":      "EPS Revisions",
        "short_interest": "Short Interest",
    },
    "SECTORS": {
        "XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Health Care",
        "XLY": "Cons. Discretionary", "XLP": "Cons. Staples", "XLI": "Industrials",
        "XLB": "Materials", "XLU": "Utilities", "XLRE": "Real Estate", "XLC": "Communication",
    },
    "HISTORY_MONTHS": 26,          # backfill depth (24 full months for the monthly heatmap + slack)
    "REFRESH_OVERLAP_DAYS": 15,    # calendar days re-downloaded on every refresh
    "ADJ_DRIFT_PCT": 0.3,          # stored vs new close differ more than this -> re-backfill ticker
    "ZSCORE_WINDOW": 60,           # trailing days (today excluded)
    "ZSCORE_MIN_OBS": 40,
    "RS_RATIO_WINDOW": 63,         # RRG: RS-Ratio = 100 * RS / SMA(RS, 63)
    "RS_MOM_LAG": 10,              # RRG: RS-Momentum = 100 * RS-Ratio / RS-Ratio 10 days ago
    "RRG_TAIL": 10,                # trail points per sector on the rotation graph
    "PERIODS": {"1D": 1, "5D": 5, "1M": 21, "3M": 63},   # + MTD / YTD (calendar)
    "HEATMAP_DAYS": 10,
    "CUM_WINDOWS": [5, 20, 60],
    "MONTHS_SHOWN": 24,
    "WEEKS_SHOWN": 26,
    "MONTH_RANK_JUMP": 7,          # rank moved >= this many places vs the previous bucket -> flag
    "FLIP_MIN": {"M": 0.03, "W": 0.015},  # ...or sign flipped with both buckets at least this big
    "BUCKET_REGIME_MIN_SHARE": 0.25,  # a month/week takes a non-neutral regime only if it held
                                      # on >= this share of its days (else Neutral)
    "REGIME": {
        "ZSCORE_BASIS": "1d",      # '1d' = z of the daily spread (spec) | '5d' = z of the 5-day spread
        "MIN_DAYS": 1,             # consecutive days a new regime must hold before it counts
        # Checked in this order; first match wins.  (factor, op, threshold) on z-scores.
        "RULES": [
            ("momentum_crash", [("momentum", "<", -2.0)]),
            ("defensive", [("low_vol", ">", 1.5), ("value", ">", 1.5),
                           ("growth", "<", -1.0), ("momentum", "<", -1.0)]),
            ("risk_on",   [("low_vol", "<", -1.5), ("value", "<", -1.5),
                           ("growth", ">", 1.0), ("momentum", ">", 1.0)]),
        ],
        "LABELS": {"defensive": "Defensive", "risk_on": "Risk-on",
                   "momentum_crash": "Momentum crash", "neutral": "Neutral"},
    },
    # Extension points (NOT built): future sources write into rotation_ext_series
    # (source, series_id, date, value) through a loader registered in EXTENSION_LOADERS.
    "EXTENSIONS": {
        "13f":   {"label": "13F institutional positioning (quarterly, SEC)", "status": "planned"},
        "finra_si": {"label": "FINRA short interest (twice monthly)", "status": "planned"},
        "cftc_cot": {"label": "CFTC Commitments of Traders (weekly, futures)", "status": "planned"},
    },
}

EXTENSION_LOADERS = {}   # source id -> callable(conn) -> rows written; register future loaders here


def all_tickers(cfg=CONFIG):
    t = {cfg["BENCHMARK"], *cfg["SECTORS"]}
    for f in cfg["FACTORS"].values():
        t.update((f["long"], f["short"]))
    return sorted(t)


def series_defs(cfg=CONFIG):
    """[(series_id, kind, label, long, short)] — factors then sectors (vs benchmark)."""
    out = [(k, "factor", f["label"], f["long"], f["short"]) for k, f in cfg["FACTORS"].items()]
    out += [(t, "sector", name, t, cfg["BENCHMARK"]) for t, name in cfg["SECTORS"].items()]
    return out


# ---------------------------------------------------------------------------
# Pure calculations (no DB / network) — covered by scripts/test_rotation.py
# ---------------------------------------------------------------------------
def pair_returns(close, long, short):
    """close: DataFrame (date index ascending, ticker columns, NaN = no close).
    Returns DataFrame [long_ret, short_ret, spread] on dates where BOTH have a close; the
    first common date has no return.  A day missing for one ETF folds into the next
    common day's return (never zero-filled, never forward-filled)."""
    if long not in close or short not in close:
        return pd.DataFrame(columns=["long_ret", "short_ret", "spread"], dtype=float)
    sub = close[[long, short]].dropna() if long != short else close[[long]].dropna()
    lr = sub[long].pct_change()
    sr = sub[short].pct_change()
    out = pd.DataFrame({"long_ret": lr, "short_ret": sr})
    out["spread"] = out["long_ret"] - out["short_ret"]
    return out.iloc[1:]


def compound(returns):
    """Cumulative return of a sequence of simple daily returns (daily-rebalanced spread)."""
    r = pd.Series(returns, dtype=float).dropna()
    return float(np.prod(1.0 + r.values) - 1.0) if len(r) else None


def rolling_compound(spread, n):
    return (1.0 + spread).rolling(n, min_periods=n).apply(np.prod, raw=True) - 1.0


def trailing_zscore(x, window=60, min_obs=40):
    """z_t = (x_t - mean(x_{t-window..t-1})) / std(...): today excluded -> no look-ahead."""
    prior = x.shift(1).rolling(window, min_periods=min_obs)
    sd = prior.std(ddof=1)
    z = (x - prior.mean()) / sd.where(sd > 0)
    return z


def rs_lines(close, long, short, window=63, lag=10):
    """RRG-style RS-Ratio / RS-Momentum of long vs short (both 100-centred)."""
    sub = close[[long, short]].dropna()
    rs = sub[long] / sub[short]
    ratio = 100.0 * rs / rs.rolling(window, min_periods=window).mean()
    mom = 100.0 * ratio / ratio.shift(lag)
    return pd.DataFrame({"rs_ratio": ratio, "rs_momentum": mom})


def rrg_quadrant(rs_ratio, rs_momentum):
    if rs_ratio is None or rs_momentum is None or pd.isna(rs_ratio) or pd.isna(rs_momentum):
        return None
    if rs_ratio >= 100:
        return "Leading" if rs_momentum >= 100 else "Weakening"
    return "Improving" if rs_momentum >= 100 else "Lagging"


def period_aggregate(spread, freq):
    """Compound daily spreads into calendar buckets. freq 'M' (calendar month, key YYYY-MM)
    or 'W' (week ending Friday, key = that Friday's date). Returns Series key -> return,
    plus Series key -> number of trading days in the bucket."""
    s = spread.dropna()
    if s.empty:
        return pd.Series(dtype=float), pd.Series(dtype=int)
    idx = pd.to_datetime(s.index)
    if freq == "M":
        keys = idx.strftime("%Y-%m")
    else:
        keys = (idx + pd.to_timedelta((4 - idx.weekday) % 7, unit="D")).strftime("%Y-%m-%d")
    g = pd.Series(s.values, index=keys).groupby(level=0)
    return g.apply(lambda v: float(np.prod(1.0 + v.values) - 1.0)), g.size()


def rank_desc(values):
    """{id: value} -> {id: rank} (1 = highest; None values unranked)."""
    ok = sorted(((v, k) for k, v in values.items() if v is not None and not pd.isna(v)),
                key=lambda x: -x[0])
    return {k: i + 1 for i, (_, k) in enumerate(ok)}


def _cmp(v, op, thr):
    if v is None or pd.isna(v):
        return False
    return v > thr if op == ">" else v < thr if op == "<" else v >= thr if op == ">=" else v <= thr


def classify_regime(z, rules=None):
    """z: {factor_id: zscore} for one day -> regime id ('neutral' if no rule matches)."""
    for name, conds in (rules if rules is not None else CONFIG["REGIME"]["RULES"]):
        if conds and all(_cmp(z.get(f), op, thr) for f, op, thr in conds):
            return name
    return "neutral"


def regime_episodes(daily, min_days=1):
    """daily: list of (date, raw_regime, detail) ascending.  A new regime counts once it held
    `min_days` consecutive days; it is dated from its FIRST day (known only after min_days —
    the history is descriptive, the live 'current regime' is only switched when confirmed).
    Returns [{regime, start_date, end_date (None = current), days, detail}]."""
    eps, cur = [], None
    run_reg, run_start, run_len, run_detail = None, None, 0, None
    for d, reg, detail in daily:
        if reg == run_reg:
            run_len += 1
        else:
            run_reg, run_start, run_len, run_detail = reg, d, 1, detail
        if cur is None:
            cur = {"regime": reg, "start_date": d, "end_date": None, "days": 0, "detail": detail}
            eps.append(cur)
        elif reg != cur["regime"] and run_len >= min_days:
            # close the previous episode on the day before the new run started
            prev_days = [x for x, _, _ in daily if cur["start_date"] <= x < run_start]
            cur["end_date"] = prev_days[-1] if prev_days else cur["start_date"]
            cur = {"regime": reg, "start_date": run_start, "end_date": None, "days": 0,
                   "detail": run_detail}
            eps.append(cur)
    dates = [d for d, _, _ in daily]
    for e in eps:
        end = e["end_date"] or (dates[-1] if dates else e["start_date"])
        e["days"] = sum(1 for d in dates if e["start_date"] <= d <= end)
    return eps


def compute_all(close, cfg=CONFIG):
    """close (wide adj-close DataFrame, string-date index) -> (series_df, daily_regimes).
    series_df columns: series_id, kind, date, long_ret, short_ret, spread, zscore, rs_ratio, rs_momentum."""
    reg_cfg = cfg["REGIME"]
    frames, z_for_regime = [], {}
    for sid, kind, _label, lg, sh in series_defs(cfg):
        pr = pair_returns(close, lg, sh)
        if pr.empty:
            continue
        basis = pr["spread"] if reg_cfg["ZSCORE_BASIS"] == "1d" else rolling_compound(pr["spread"], 5)
        pr["zscore"] = trailing_zscore(basis, cfg["ZSCORE_WINDOW"], cfg["ZSCORE_MIN_OBS"])
        rs = rs_lines(close, lg, sh, cfg["RS_RATIO_WINDOW"], cfg["RS_MOM_LAG"])
        pr = pr.join(rs, how="left")
        pr["series_id"], pr["kind"] = sid, kind
        frames.append(pr.reset_index(names="date"))
        if kind == "factor":
            z_for_regime[sid] = pr["zscore"]
    cols = ["series_id", "kind", "date", "long_ret", "short_ret", "spread", "zscore", "rs_ratio", "rs_momentum"]
    series = pd.concat(frames, ignore_index=True)[cols] if frames else pd.DataFrame(columns=cols)
    daily = []
    if z_for_regime:
        zdf = pd.DataFrame(z_for_regime)
        needed = {f for _, conds in reg_cfg["RULES"] for f, _, _ in conds}
        for d, row in zdf.iterrows():
            z = {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}
            if any(z.get(f) is None for f in needed if f in zdf.columns):
                continue                                   # not enough history for a z yet
            detail = {f: round(z[f], 2) for f in sorted(needed) if z.get(f) is not None}
            daily.append((d, classify_regime(z, reg_cfg["RULES"]), detail))
    return series, daily


# ---------------------------------------------------------------------------
# Data: download + store raw prices
# ---------------------------------------------------------------------------
def _download(tickers, start):
    """Adjusted daily closes from Yahoo. Returns {ticker: DataFrame[close, volume]}; tickers
    that failed or came back empty are simply missing."""
    import yfinance as yf
    out = {}
    if not tickers:
        return out
    try:
        df = yf.download(list(tickers), start=start, interval="1d", auto_adjust=True,
                         actions=False, threads=True, progress=False, group_by="column")
    except Exception as exc:
        log.warning("rotation download failed: %s", exc)
        return out
    if df is None or df.empty:
        return out
    for t in tickers:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                c, v = df[("Close", t)], df[("Volume", t)]
            else:
                c, v = df["Close"], df["Volume"]
        except KeyError:
            continue
        sub = pd.DataFrame({"close": c, "volume": v}).dropna(subset=["close"])
        if not sub.empty:
            sub.index = pd.to_datetime(sub.index).strftime("%Y-%m-%d")
            out[t] = sub
    return out


def _drop_forming_bar(frames):
    """Remove today's bar until the US close is final — a partial bar is not a close."""
    try:
        from surge_strategy import market_clock
        clk = market_clock()
    except Exception:
        return frames
    if clk["is_final"]:
        return frames
    return {t: f[f.index < clk["today"]] for t, f in frames.items()}


def _store(conn, frames):
    now = datetime.now().isoformat(timespec="seconds")
    rows = [(t, d, float(r.close), None if pd.isna(r.volume) else float(r.volume), now)
            for t, f in frames.items() for d, r in f.iterrows()]
    conn.executemany(
        "INSERT INTO rotation_prices (ticker, date, close, volume, fetched_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(ticker, date) DO UPDATE SET close=excluded.close, volume=excluded.volume, "
        "fetched_at=excluded.fetched_at", rows)
    return len(rows)


def _set_meta(conn, **kv):
    for k, v in kv.items():
        conn.execute("INSERT INTO rotation_meta (key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (k, v if isinstance(v, str) else json.dumps(v)))


def get_meta(conn):
    out = {}
    for r in conn.execute("SELECT key, value FROM rotation_meta"):
        try:
            out[r["key"]] = json.loads(r["value"])
        except (TypeError, ValueError):
            out[r["key"]] = r["value"]
    return out


def _history_start(cfg=CONFIG):
    d = date.today().replace(day=1)
    m = d.month - cfg["HISTORY_MONTHS"]
    y = d.year + (m - 1) // 12
    return date(y, (m - 1) % 12 + 1, 1).isoformat()


_lock = threading.Lock()


def backfill(tickers=None, cfg=CONFIG):
    """Full history download (replaces the stored rows of these tickers)."""
    tickers = tickers or all_tickers(cfg)
    frames = _drop_forming_bar(_download(tickers, _history_start(cfg)))
    with get_connection() as conn:
        for t in frames:
            conn.execute("DELETE FROM rotation_prices WHERE ticker=?", (t,))
        n = _store(conn, frames)
        conn.commit()
    failed = sorted(set(tickers) - set(frames))
    log.info("rotation backfill: %d rows, %d tickers, failed=%s", n, len(frames), failed)
    return {"rows": n, "ok": sorted(frames), "failed": failed}


def refresh(cfg=CONFIG, recompute_after=True):
    """Incremental daily refresh. Backfills tickers with no history; re-backfills a ticker
    whose stored closes moved more than ADJ_DRIFT_PCT (Yahoo re-adjusted it after a dividend)."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        setup_database()
        tickers = all_tickers(cfg)
        with get_connection() as conn:
            have = {r[0]: r[1] for r in conn.execute(
                "SELECT ticker, MAX(date) FROM rotation_prices GROUP BY ticker")}
        missing = [t for t in tickers if t not in have]
        res = {"backfilled": [], "rebackfilled": [], "failed": [], "rows": 0}
        if missing:
            b = backfill(missing, cfg)
            res["backfilled"], res["failed"], res["rows"] = b["ok"], b["failed"], b["rows"]
        current = [t for t in tickers if t in have]
        if current:
            start = (datetime.strptime(min(have[t] for t in current), "%Y-%m-%d")
                     - timedelta(days=cfg["REFRESH_OVERLAP_DAYS"])).date().isoformat()
            frames = _drop_forming_bar(_download(current, start))
            drift = []
            with get_connection() as conn:
                for t, f in frames.items():
                    old = dict(conn.execute("SELECT date, close FROM rotation_prices WHERE ticker=? "
                                            "AND date>=?", (t, f.index[0])).fetchall())
                    for d, r in f.iterrows():
                        if d in old and old[d] and abs(r.close / old[d] - 1) * 100 > cfg["ADJ_DRIFT_PCT"]:
                            drift.append(t)
                            break
                res["rows"] += _store(conn, {t: f for t, f in frames.items() if t not in drift})
                conn.commit()
            if drift:
                b = backfill(drift, cfg)
                res["rebackfilled"] = b["ok"]
                res["failed"] += b["failed"]
            res["failed"] += sorted(set(current) - set(frames))
        res["failed"] = sorted(set(res["failed"]))
        with get_connection() as conn:
            last_ok = {r[0]: r[1] for r in conn.execute(
                "SELECT ticker, MAX(date) FROM rotation_prices GROUP BY ticker")}
            _set_meta(conn, last_refresh=datetime.now().isoformat(timespec="seconds"),
                      failed={t: last_ok.get(t) for t in res["failed"]})
            conn.commit()
        if recompute_after:
            res.update(recompute(cfg))
        res["status"] = "ok"
        return res
    finally:
        _lock.release()


# ---------------------------------------------------------------------------
# Recompute: prices -> rotation_series + rotation_regimes
# ---------------------------------------------------------------------------
def load_close(conn, tickers=None):
    q = "SELECT ticker, date, close FROM rotation_prices"
    df = pd.read_sql_query(q, conn)
    if tickers:
        df = df[df["ticker"].isin(tickers)]
    if df.empty:
        return pd.DataFrame()
    return df.pivot(index="date", columns="ticker", values="close").sort_index()


def recompute(cfg=CONFIG):
    with get_connection() as conn:
        close = load_close(conn, all_tickers(cfg))
    if close.empty:
        return {"series_rows": 0, "as_of": None}
    series, daily = compute_all(close, cfg)
    eps = regime_episodes(daily, cfg["REGIME"]["MIN_DAYS"])
    now = datetime.now().isoformat(timespec="seconds")

    def _f(v):
        return None if v is None or pd.isna(v) else float(v)

    with get_connection() as conn:
        conn.execute("DELETE FROM rotation_series")
        conn.executemany(
            "INSERT INTO rotation_series (series_id, date, kind, long_ret, short_ret, spread, zscore, "
            "rs_ratio, rs_momentum) VALUES (?,?,?,?,?,?,?,?,?)",
            [(r.series_id, r.date, r.kind, _f(r.long_ret), _f(r.short_ret), _f(r.spread),
              _f(r.zscore), _f(r.rs_ratio), _f(r.rs_momentum)) for r in series.itertuples()])
        conn.execute("DELETE FROM rotation_regimes")
        conn.executemany(
            "INSERT INTO rotation_regimes (regime, start_date, end_date, days, detail, detected_at) "
            "VALUES (?,?,?,?,?,?)",
            [(e["regime"], e["start_date"], e["end_date"], e["days"], json.dumps(e["detail"]), now)
             for e in eps])
        as_of = series["date"].max() if len(series) else None
        _set_meta(conn, as_of=as_of or "", last_recompute=now)
        conn.commit()
    return {"series_rows": len(series), "as_of": as_of, "regime_episodes": len(eps)}


# ---------------------------------------------------------------------------
# Read side (API)
# ---------------------------------------------------------------------------
def _load_series(conn):
    df = pd.read_sql_query("SELECT * FROM rotation_series ORDER BY date", conn)
    return {sid: g.set_index("date") for sid, g in df.groupby("series_id")}


def _r(v, n=6):
    return None if v is None or pd.isna(v) else round(float(v), n)


def _periods(spread, as_of, cfg=CONFIG):
    out = {k: compound(spread.iloc[-n:]) if len(spread) >= n else None
           for k, n in cfg["PERIODS"].items()}
    out["MTD"] = compound(spread[spread.index >= as_of[:7] + "-01"])
    out["YTD"] = compound(spread[spread.index >= as_of[:4] + "-01-01"])
    return out


def summary(cfg=CONFIG):
    """Everything the page's main tables need for the latest day."""
    with get_connection() as conn:
        data = _load_series(conn)
        meta = get_meta(conn)
        regs = [dict(r) for r in conn.execute("SELECT * FROM rotation_regimes ORDER BY start_date")]
    if not data:
        return {"as_of": None, "factors": [], "sectors": [], "meta": meta,
                "unavailable": cfg["UNAVAILABLE"], "extensions": cfg["EXTENSIONS"]}
    as_of = max(g.index.max() for g in data.values())
    rows = {}
    for sid, kind, label, lg, sh in series_defs(cfg):
        g = data.get(sid)
        if g is None or g.empty:
            rows[sid] = {"id": sid, "kind": kind, "label": label, "long": lg, "short": sh, "missing": True}
            continue
        sp = g["spread"].dropna()
        last = g.iloc[-1]
        row = {"id": sid, "kind": kind, "label": label, "long": lg, "short": sh,
               "date": g.index[-1], "stale": g.index[-1] < as_of,
               "long_ret": _r(last["long_ret"]), "short_ret": _r(last["short_ret"]),
               "z": _r(last["zscore"], 2),
               "periods": {k: _r(v) for k, v in _periods(sp, as_of, cfg).items()},
               "cum": {f"{n}D": _r(compound(sp.iloc[-n:])) for n in cfg["CUM_WINDOWS"]},
               "heat": [[d, _r(v)] for d, v in sp.iloc[-cfg["HEATMAP_DAYS"]:].items()],
               "rs_ratio": _r(last["rs_ratio"], 2), "rs_momentum": _r(last["rs_momentum"], 2)}
        row["quadrant"] = rrg_quadrant(row["rs_ratio"], row["rs_momentum"])
        tail = g[["rs_ratio", "rs_momentum"]].dropna().iloc[-cfg["RRG_TAIL"]:]
        row["rrg_tail"] = [[_r(a, 2), _r(b, 2)] for a, b in tail.values]
        rows[sid] = row
    # daily 1D ranks: today, yesterday, 5 days ago (within each kind)
    for kind in ("factor", "sector"):
        ids = [s for s, k, *_ in series_defs(cfg) if k == kind and not rows[s].get("missing")]
        ranks = []
        for back in (0, 1, 5):
            vals = {}
            for s in ids:
                sp = data[s]["spread"].dropna()
                vals[s] = sp.iloc[-1 - back] if len(sp) > back else None
            ranks.append(rank_desc(vals))
        for s in ids:
            r0, r1, r5 = (ranks[i].get(s) for i in range(3))
            rows[s]["rank"] = r0
            rows[s]["rank_chg_1d"] = (r1 - r0) if r0 and r1 else None     # + = moved up
            rows[s]["rank_chg_5d"] = (r5 - r0) if r0 and r5 else None
    cur = regs[-1] if regs else None
    last_signal = next((r for r in reversed(regs) if r["regime"] != "neutral"), None)
    for r in (cur, last_signal):
        if r and isinstance(r.get("detail"), str):
            r["detail"] = json.loads(r["detail"])
    return {
        "as_of": as_of, "meta": meta,
        "factors": [rows[s] for s, k, *_ in series_defs(cfg) if k == "factor"],
        "sectors": [rows[s] for s, k, *_ in series_defs(cfg) if k == "sector"],
        "regime": cur, "last_signal": last_signal,
        "regime_labels": cfg["REGIME"]["LABELS"],
        "regime_config": {"basis": cfg["REGIME"]["ZSCORE_BASIS"], "min_days": cfg["REGIME"]["MIN_DAYS"],
                          "rules": cfg["REGIME"]["RULES"]},
        "unavailable": cfg["UNAVAILABLE"], "extensions": cfg["EXTENSIONS"],
        "benchmark": cfg["BENCHMARK"], "zscore_window": cfg["ZSCORE_WINDOW"],
        "flag_rule": {"jump": cfg["MONTH_RANK_JUMP"], **cfg["FLIP_MIN"]},
    }


_RANGE_DAYS = {"1M": 21, "3M": 63, "6M": 126, "1Y": 252, "2Y": 504}


def cumulative_series(kind="factor", rng="3M", cfg=CONFIG):
    """Cumulative (compounded) spread of each series over the last `rng`, rebased to 0."""
    n = _RANGE_DAYS.get(rng, 63)
    with get_connection() as conn:
        data = _load_series(conn)
    defs = [d for d in series_defs(cfg) if d[1] == kind and d[0] in data]
    if not defs:
        return {"dates": [], "series": []}
    dates = sorted(set().union(*(data[s].index for s, *_ in defs)))[-n:]
    out = []
    for sid, _k, label, *_ in defs:
        sp = data[sid]["spread"].reindex(dates)
        cum = (1.0 + sp.fillna(0.0)).cumprod() - 1.0      # missing day = flat (no return known)
        fv = sp.first_valid_index()
        cum[[d < fv for d in cum.index] if fv is not None else slice(None)] = np.nan
        out.append({"id": sid, "label": label, "values": [_r(v) for v in cum.values]})
    return {"dates": dates, "series": out, "range": rng}


def calendar(freq="monthly", cfg=CONFIG):
    """Monthly (24 months) or weekly (26 weeks) heatmap for factors + sectors, with ranks,
    rank change vs the previous bucket, flags and MTD/YTD. The newest bucket is partial."""
    f = "M" if freq == "monthly" else "W"
    n = cfg["MONTHS_SHOWN"] if f == "M" else cfg["WEEKS_SHOWN"]
    with get_connection() as conn:
        data = _load_series(conn)
        regs = [dict(r) for r in conn.execute("SELECT regime, start_date, end_date FROM rotation_regimes")]
    if not data:
        return {"buckets": [], "factors": [], "sectors": []}
    as_of = max(g.index.max() for g in data.values())
    agg = {}
    for sid, *_ in series_defs(cfg):
        if sid in data:
            agg[sid] = period_aggregate(data[sid]["spread"], f)[0]
    all_keys = sorted(set().union(*(a.index for a in agg.values())))
    keys = all_keys[-(n + 1):]          # one extra bucket for the first rank change
    shown = keys[-n:]
    out = {"buckets": shown, "freq": freq, "as_of": as_of,
           "partial": shown[-1] if shown and ((f == "M" and not _month_complete(as_of)) or
                                              (f == "W" and shown[-1] > as_of)) else None}
    for kind in ("factor", "sector"):
        ids = [s for s, k, *_ in series_defs(cfg) if k == kind and s in agg]
        ranks = {k: rank_desc({s: agg[s].get(k) for s in ids}) for k in keys}
        rows = []
        for sid, k_, label, lg, sh in series_defs(cfg):
            if k_ != kind or sid not in agg:
                continue
            cells = []
            for i, k in enumerate(keys):
                if k not in shown:
                    continue
                v = agg[sid].get(k)
                rk = ranks[k].get(sid)
                prev_k = keys[i - 1] if i > 0 else None
                prk = ranks[prev_k].get(sid) if prev_k else None
                pv = agg[sid].get(prev_k) if prev_k else None
                chg = (prk - rk) if rk and prk else None
                flag = bool(chg is not None and abs(chg) >= cfg["MONTH_RANK_JUMP"]) or bool(
                    v is not None and pv is not None and np.sign(v) != np.sign(pv)
                    and abs(v) >= cfg["FLIP_MIN"][f] and abs(pv) >= cfg["FLIP_MIN"][f])
                cells.append({"v": _r(v), "rank": rk, "rank_chg": chg, "flag": flag})
            sp = data[sid]["spread"].dropna()
            rows.append({"id": sid, "label": label, "long": lg, "short": sh, "cells": cells,
                         "MTD": _r(compound(sp[sp.index >= as_of[:7] + "-01"])),
                         "YTD": _r(compound(sp[sp.index >= as_of[:4] + "-01-01"]))})
        out[kind + "s"] = rows
    # dominant regime per bucket (majority of days, neutral only when nothing else)
    out["bucket_regime"] = _bucket_regimes(regs, data, shown, f)
    return out


def _month_complete(as_of):
    d = datetime.strptime(as_of, "%Y-%m-%d").date()
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.month != d.month


def _bucket_regimes(regs, data, keys, f):
    any_series = next(iter(data.values()))
    dates = list(any_series.index)
    day_reg = {}
    for r in regs:
        end = r["end_date"] or dates[-1]
        for d in dates:
            if r["start_date"] <= d <= end:
                day_reg[d] = r["regime"]
    out, prev = [], None
    for k in keys:
        if f == "M":
            ds = [d for d in dates if d[:7] == k]
        else:
            fri = datetime.strptime(k, "%Y-%m-%d").date()
            ds = [d for d in dates if (fri - timedelta(days=6)).isoformat() < d <= k]
        counts = {}
        for d in ds:
            if d in day_reg:
                counts[day_reg[d]] = counts.get(day_reg[d], 0) + 1
        non_neutral = {a: b for a, b in counts.items() if a != "neutral"}
        top = max(non_neutral, key=non_neutral.get) if non_neutral else None
        total = sum(counts.values())
        if top and non_neutral[top] >= CONFIG["BUCKET_REGIME_MIN_SHARE"] * total:
            dom = top
        else:
            dom = "neutral" if counts else None
        out.append({"key": k, "regime": dom, "days": counts,
                    "changed": prev is not None and dom is not None and dom != prev})
        prev = dom if dom is not None else prev
    return out


def regime_history():
    with get_connection() as conn:
        regs = [dict(r) for r in conn.execute("SELECT * FROM rotation_regimes ORDER BY start_date DESC")]
    for r in regs:
        try:
            r["detail"] = json.loads(r["detail"] or "{}")
        except ValueError:
            pass
    return {"history": regs, "labels": CONFIG["REGIME"]["LABELS"],
            "config": {"basis": CONFIG["REGIME"]["ZSCORE_BASIS"], "min_days": CONFIG["REGIME"]["MIN_DAYS"],
                       "rules": CONFIG["REGIME"]["RULES"]}}


def show():
    s = summary()
    if not s["as_of"]:
        print("No rotation data — run: python rotation.py --backfill")
        return
    print(f"Factor & Sector Rotation (ETF proxies) - latest trading day {s['as_of']}"
          f"  (last refresh {s['meta'].get('last_refresh')})")
    reg = s.get("regime") or {}
    print(f"Regime: {CONFIG['REGIME']['LABELS'].get(reg.get('regime'), '-')} since {reg.get('start_date')}"
          f"  z={reg.get('detail')}")
    for kind in ("factors", "sectors"):
        rows = sorted([r for r in s[kind] if not r.get("missing")],
                      key=lambda r: -(r["periods"]["1D"] or -9))
        print(f"\n{kind.upper():<22}{'pair':<12}{'1D':>8}{'5D':>8}{'1M':>8}{'3M':>8}{'YTD':>8}{'z':>7}"
              f"{'rk':>4}{'chg':>5}  quadrant")
        for r in rows:
            p = r["periods"]
            fmt = lambda v: f"{v*100:+7.2f}%" if v is not None else "     - "
            print(f"{r['label'][:21]:<22}{(r['long']+'-'+r['short']):<12}{fmt(p['1D'])}{fmt(p['5D'])}"
                  f"{fmt(p['1M'])}{fmt(p['3M'])}{fmt(p['YTD'])}"
                  f"{(r['z'] if r['z'] is not None else float('nan')):7.2f}{r.get('rank') or '-':>4}"
                  f"{(r.get('rank_chg_1d') or 0):+5d}  {r.get('quadrant') or ''}")
    for k, v in s["unavailable"].items():
        print(f"  {v}: not available (no free ETF proxy)")
    if s["meta"].get("failed"):
        print("Failed downloads (last good date):", s["meta"]["failed"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="Factor & Sector Rotation (ETF proxies)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--backfill", action="store_true")
    g.add_argument("--refresh", action="store_true")
    g.add_argument("--recompute", action="store_true")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    setup_database()
    if a.backfill:
        print(backfill())
        with get_connection() as c:
            _set_meta(c, last_refresh=datetime.now().isoformat(timespec="seconds"))
            c.commit()
        print(recompute())
    elif a.refresh:
        print(refresh())
    elif a.recompute:
        print(recompute())
    show()
