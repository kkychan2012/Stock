"""
Flag volume surges caused by a confirmed acquisition, using SEC EDGAR filings.

A takeover announcement produces exactly the kind of day the Volume Surge strategy
looks for (huge volume, big up day) but the stock then sits pinned under the deal
price -- there is nothing left to trade. Price data alone cannot tell such a day
from an earnings surge, so this looks the company up on EDGAR and checks whether
a merger / tender-offer document was filed around the surge day:

    PREM14A / DEFM14A / PREM14C / DEFM14C   merger proxy (shareholder vote on the deal)
    SC TO-T / SC TO-C / SC 14D9 / SC14D9C   tender offer (by the buyer / target's response)
    SC 13E3                                 going-private transaction
    425                                     merger communication (stock deals; filed by either side)

A surge is flagged when one of those forms is filed from WINDOW_BEFORE days before
it to WINDOW_AFTER days after it. The days after are needed because only tender-offer
and 425 filings appear on the announcement day; a merger proxy follows weeks later.
In the backtest that is a stand-in for what you would have read in the news on the
surge day, not information the strategy trades on.

Limits: a company that is the BUYER in a deal is flagged too (it files 425 / S-4
material); an unrelated surge that happens to fall inside the window of a real deal
is flagged as well; a deal announced by press release only and abandoned before any
of these forms was filed is missed. Tickers with no SEC match (rare) are never flagged.

Filings are cached in us_backtest_10y/sec_merger_filings.json, so only the first
run is slow. CLI:  python scripts/acquisition_filter.py VMW 2022-05-23
"""
import json
import os
import re
import sys
import time
from datetime import date, timedelta

import requests

MERGER_FORMS = {"PREM14A", "DEFM14A", "PREM14C", "DEFM14C", "SC TO-T", "SC TO-C", "SC 14D9", "SC14D9C",
                "SC 13E3", "425"}
WINDOW_BEFORE = 5     # calendar days
WINDOW_AFTER = 75
CACHE_PATH = os.path.join("us_backtest_10y", "sec_merger_filings.json")
EARLIEST = "2015-01-01"   # older filing pages are not downloaded
_PAUSE = 0.15             # SEC asks for <= 10 requests/second


def _user_agent():
    """SEC requires a declared User-Agent; reuse the one already configured for the Insider pipeline."""
    cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "Insider Trading", "insider_pipeline", "config.py")
    m = re.search(r'^SEC_USER_AGENT\s*=\s*["\'](.+?)["\']', open(cfg, encoding="utf-8").read(), re.M)
    if not m:
        raise RuntimeError("SEC_USER_AGENT not found in Insider Trading/insider_pipeline/config.py")
    return m.group(1)


class AcquisitionFilter:
    def __init__(self, cache_path=CACHE_PATH):
        self.cache_path = cache_path
        self.cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
        self._session = None
        self._cik = None

    # ---- EDGAR ----
    def _get(self, url):
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": _user_agent(), "Accept-Encoding": "gzip, deflate"})
        for attempt in range(4):
            time.sleep(_PAUSE)
            r = self._session.get(url, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            time.sleep(2 * (attempt + 1))            # 429 / 5xx: back off and retry
        r.raise_for_status()

    def _cik_map(self):
        if self._cik is None:
            data = self._get("https://www.sec.gov/files/company_tickers.json") or {}
            self._cik = {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}
        return self._cik

    def _fetch(self, ticker):
        cik = self._cik_map().get(ticker.upper())
        if cik is None:
            return None
        sub = self._get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        if not sub:
            return None
        pages = [sub["filings"]["recent"]]
        for f in sub["filings"].get("files", []):
            if f.get("filingTo", "9999") >= EARLIEST:
                page = self._get(f"https://data.sec.gov/submissions/{f['name']}")
                if page:
                    pages.append(page)
        found = set()
        for p in pages:
            for form, filed in zip(p["form"], p["filingDate"]):
                base = form[:-2] if form.endswith("/A") else form
                if base in MERGER_FORMS and filed >= EARLIEST:
                    found.add((filed, base))
        return sorted(found)

    def load(self, tickers, progress=True):
        """Make sure every ticker's merger filings are cached (downloads the missing ones)."""
        todo = [t for t in sorted(set(tickers)) if t not in self.cache]
        for n, t in enumerate(todo, 1):
            try:
                self.cache[t] = self._fetch(t)
            except Exception as e:                    # leave it uncached so the next run retries
                print(f"  SEC lookup failed for {t}: {e}")
                continue
            if progress and (n % 50 == 0 or n == len(todo)):
                print(f"  SEC filings: {n}/{len(todo)} tickers", flush=True)
                self._save()
        if todo:
            self._save()

    def _save(self):
        os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
        with open(self.cache_path, "w") as f:
            json.dump(self.cache, f)

    # ---- the check ----
    def match(self, ticker, surge_date):
        """The merger filing (date, form) that flags this surge, or None."""
        filings = self.cache.get(ticker)
        if not filings:
            return None
        d = date.fromisoformat(surge_date)
        lo, hi = (d - timedelta(days=WINDOW_BEFORE)).isoformat(), (d + timedelta(days=WINDOW_AFTER)).isoformat()
        for filed, form in filings:
            if lo <= filed <= hi:
                return filed, form
        return None

    def known(self, ticker):
        return self.cache.get(ticker) is not None


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python scripts/acquisition_filter.py TICKER SURGE_DATE")
        sys.exit(1)
    af = AcquisitionFilter()
    af.load([sys.argv[1].upper()], progress=False)
    print("filings:", af.cache.get(sys.argv[1].upper()))
    print("match:", af.match(sys.argv[1].upper(), sys.argv[2]))
