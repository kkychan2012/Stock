"""
Tests for rotation.py's pure calculations: spread, compounding, trailing z-score (no
look-ahead), calendar-month / weekly aggregation, ranks, RRG quadrant and regime logic.
No network, no DB.

Run from the project root:  python scripts/test_rotation.py
"""
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rotation as rot

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    fails += 0 if cond else 1


def close_df(data, dates):
    return pd.DataFrame(data, index=dates)


# --- spread ---------------------------------------------------------------
d = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
c = close_df({"A": [100, 110, 99, 99], "B": [50, 50, 55, 55]}, d)
pr = rot.pair_returns(c, "A", "B")
check("spread: first day has no return (dropped)", list(pr.index) == d[1:])
check("spread day 2 = +10% - 0%", math.isclose(pr.loc["2026-01-06", "spread"], 0.10))
check("spread day 3 = -10% - +10%", math.isclose(pr.loc["2026-01-07", "spread"], -0.20))

# missing day for one ETF folds into the next common day (no zero-fill, no ffill)
c2 = close_df({"A": [100, np.nan, 121, 121], "B": [50, 55, 55, 60.5]}, d)
pr2 = rot.pair_returns(c2, "A", "B")
check("missing day: skipped date absent", "2026-01-06" not in pr2.index)
check("missing day: 2-day return on next common day", math.isclose(pr2.loc["2026-01-07", "long_ret"], 0.21)
      and math.isclose(pr2.loc["2026-01-07", "short_ret"], 0.10))
check("missing ticker -> empty", rot.pair_returns(c, "A", "ZZZ").empty)

# --- compounding ----------------------------------------------------------
check("compound(+10%, -10%) = -1%", math.isclose(rot.compound([0.1, -0.1]), -0.01))
check("compound(empty) = None", rot.compound([]) is None)
check("compound ignores NaN", math.isclose(rot.compound([0.1, np.nan]), 0.1))
rc = rot.rolling_compound(pd.Series([0.1, 0.1, 0.1]), 2)
check("rolling_compound 2d", math.isnan(rc.iloc[0]) and math.isclose(rc.iloc[1], 0.21))

# --- z-score: trailing window, today excluded --------------------------------
x = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 100.0])
z = rot.trailing_zscore(x, window=5, min_obs=5)
prior = np.array([1, 2, 3, 4, 5.0])
expect = (100 - prior.mean()) / prior.std(ddof=1)
check("z uses previous window only", math.isclose(z.iloc[5], expect), f"{z.iloc[5]} vs {expect}")
check("z NaN before min_obs", z.iloc[:5].isna().all())
# no look-ahead: changing a future value never changes a past z
x2 = x.copy(); x2.iloc[5] = -999
z2 = rot.trailing_zscore(pd.concat([x, pd.Series([7.0])], ignore_index=True), 5, 5)
z3 = rot.trailing_zscore(pd.concat([x, pd.Series([-500.0])], ignore_index=True), 5, 5)
check("no look-ahead: future value leaves past z unchanged", math.isclose(z2.iloc[5], z3.iloc[5]))
check("z flat window -> NaN (no divide by 0)", math.isnan(rot.trailing_zscore(
    pd.Series([1.0] * 6), 5, 5).iloc[5]))

# --- monthly / weekly aggregation ----------------------------------------------
s = pd.Series({"2026-01-29": 0.01, "2026-01-30": 0.02, "2026-02-02": -0.01, "2026-02-27": 0.03})
m, n = rot.period_aggregate(s, "M")
check("monthly keys", list(m.index) == ["2026-01", "2026-02"])
check("monthly Jan compounded", math.isclose(m["2026-01"], 1.01 * 1.02 - 1))
check("monthly Feb compounded", math.isclose(m["2026-02"], 0.99 * 1.03 - 1))
check("monthly day counts", list(n.values) == [2, 2])
w, _ = rot.period_aggregate(s, "W")
# 2026-01-29 Thu + 01-30 Fri -> week ending 2026-01-30; 02-02 Mon -> 02-06; 02-27 is a Friday
check("weekly keys = Friday", list(w.index) == ["2026-01-30", "2026-02-06", "2026-02-27"], list(w.index))
check("weekly compounded", math.isclose(w["2026-01-30"], 1.01 * 1.02 - 1))

# --- ranks ------------------------------------------------------------------
r = rot.rank_desc({"a": 0.01, "b": 0.03, "c": None, "d": -0.02})
check("rank: highest = 1, None unranked", r == {"b": 1, "a": 2, "d": 3})

# --- RRG ----------------------------------------------------------------------
check("quadrant Leading", rot.rrg_quadrant(101, 101) == "Leading")
check("quadrant Weakening", rot.rrg_quadrant(101, 99) == "Weakening")
check("quadrant Lagging", rot.rrg_quadrant(99, 99) == "Lagging")
check("quadrant Improving", rot.rrg_quadrant(99, 101) == "Improving")
check("quadrant None", rot.rrg_quadrant(None, 101) is None)
dd = pd.date_range("2025-01-01", periods=120, freq="B").strftime("%Y-%m-%d")
rising = close_df({"S": np.linspace(100, 160, 120), "B": [100.0] * 120}, dd)
rl = rot.rs_lines(rising, "S", "B", 63, 10).iloc[-1]
check("RRG: steadily outperforming -> ratio > 100", rl["rs_ratio"] > 100)
check("RRG: NaN until window filled", rot.rs_lines(rising, "S", "B", 63, 10)["rs_ratio"].iloc[:62].isna().all())

# --- regime logic -------------------------------------------------------------
rules = rot.CONFIG["REGIME"]["RULES"]
check("regime defensive", rot.classify_regime(
    {"low_vol": 1.6, "value": 1.7, "growth": -1.7, "momentum": -1.2}, rules) == "defensive")
check("regime risk-on", rot.classify_regime(
    {"low_vol": -1.6, "value": -1.6, "growth": 1.6, "momentum": 1.1}, rules) == "risk_on")
check("regime momentum crash wins over defensive", rot.classify_regime(
    {"low_vol": 2, "value": 2, "growth": -2, "momentum": -2.5}, rules) == "momentum_crash")
check("regime neutral when one leg misses", rot.classify_regime(
    {"low_vol": 1.4, "value": 1.7, "growth": -1.7, "momentum": -1.2}, rules) == "neutral")
check("regime neutral on missing z", rot.classify_regime({"low_vol": 2}, rules) == "neutral")
check("custom rule set", rot.classify_regime({"x": 3}, [("hot", [("x", ">", 2)])]) == "hot")

daily = [("d1", "neutral", {}), ("d2", "defensive", {"a": 1}), ("d3", "defensive", {}),
         ("d4", "neutral", {}), ("d5", "risk_on", {}), ("d6", "neutral", {})]
eps = rot.regime_episodes(daily, min_days=1)
check("episodes min_days=1", [e["regime"] for e in eps] ==
      ["neutral", "defensive", "neutral", "risk_on", "neutral"])
check("episode dates", eps[1]["start_date"] == "d2" and eps[1]["end_date"] == "d3" and eps[1]["days"] == 2)
check("current episode open-ended", eps[-1]["end_date"] is None and eps[-1]["days"] == 1)
check("episode keeps start-day detail", eps[1]["detail"] == {"a": 1})
eps2 = rot.regime_episodes(daily, min_days=2)
check("min_days=2: 1-day blips never switch (defensive persists)",
      [e["regime"] for e in eps2] == ["neutral", "defensive"], [e["regime"] for e in eps2])
eps3 = rot.regime_episodes(daily + [("d7", "neutral", {})], min_days=2)
check("min_days=2: 2 neutral days in a row switch, dated from the first",
      [e["regime"] for e in eps3] == ["neutral", "defensive", "neutral"]
      and eps3[2]["start_date"] == "d6" and eps3[1]["end_date"] == "d5", eps3)

# --- end to end on synthetic prices --------------------------------------------
idx = pd.date_range("2024-01-01", periods=300, freq="B").strftime("%Y-%m-%d")
rng = np.random.default_rng(1)
tick = rot.all_tickers()
px = pd.DataFrame({t: 100 * np.cumprod(1 + rng.normal(0, 0.01, len(idx))) for t in tick}, index=idx)
series, daily = rot.compute_all(px)
check("compute_all: all series present",
      set(series["series_id"]) == {s for s, *_ in rot.series_defs()})
g = series[series.series_id == "value"].set_index("date")
check("compute_all: value spread = IWD - IWF",
      math.isclose(g["spread"].iloc[-1], px["IWD"].pct_change().iloc[-1] - px["IWF"].pct_change().iloc[-1]))
gr = series[series.series_id == "growth"].set_index("date")
check("growth = -value", np.allclose(gr["spread"], -g["spread"]))
check("daily regimes start after z warm-up", len(daily) == 300 - 1 - rot.CONFIG["ZSCORE_MIN_OBS"], len(daily))

print(f"\n{'ALL PASSED' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
