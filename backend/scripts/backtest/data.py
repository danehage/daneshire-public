"""
Backtest dataset: FMP fetch with a resumable on-disk cache, point-in-time
S&P 500 membership, and a loader that turns the cache into the structures
the engine consumes.

Run from backend/ (needs FMP_API_KEY):
    python -m scripts.backtest.run --check-plan     # which endpoints our plan serves
    python -m scripts.backtest.run --fetch          # fill the cache (resumable)
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

BASE_URL = "https://financialmodelingprep.com"
DEFAULT_CACHE = Path(__file__).resolve().parent / "cache"


@dataclass
class Dataset:
    tickers: dict[str, dict]  # sym -> {sector, bars, income, balance, cashflow, earnings}
    spy: pd.DataFrame
    membership: list[tuple[date, frozenset]]  # ascending; members from date on
    universe_label: str
    survivorship_biased: bool
    coverage: dict = field(default_factory=dict)

    def members_at(self, d: date) -> frozenset:
        current = frozenset()
        for eff, members in self.membership:
            if eff <= d:
                current = members
            else:
                break
        return current


# ---------------------------------------------------------------------------
# Point-in-time membership
# ---------------------------------------------------------------------------


def build_membership(current: list[str], events: list[dict], start: date) -> list[tuple[date, frozenset]]:
    """Reconstruct S&P 500 membership back to ``start``.

    ``events`` are FMP /stable/historical-sp500-constituent rows: on ``date``
    ``symbol`` was added and ``removedTicker`` (may be blank) was removed.
    Walking back from today, each event is undone. Returns ascending
    ``(effective_date, members)``: members from that date until the next.
    """
    rows = []
    for e in events:
        d = _d(e.get("date") or e.get("dateAdded"))
        if d:
            rows.append((d, (e.get("symbol") or "").strip(), (e.get("removedTicker") or "").strip()))
    rows.sort(key=lambda r: r[0], reverse=True)

    state = set(current)
    snapshots: dict[date, frozenset] = {}
    for d, added, removed in rows:
        if d < start:
            break
        # First visit to a date sees the state after ALL of that day's
        # events — that is the membership from d onward.
        snapshots.setdefault(d, frozenset(state))
        if added:
            state.discard(added)
        if removed:
            state.add(removed)
    snapshots[date.min] = frozenset(state)
    return sorted(snapshots.items(), key=lambda r: r[0])


def _d(v) -> Optional[date]:
    if not v:
        return None
    try:
        return pd.Timestamp(v).date()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# FMP fetcher
# ---------------------------------------------------------------------------


class FMPFetcher:
    """Synchronous, throttled, retrying FMP client with a gzip JSON cache.
    Every response is cached by (endpoint, params); re-runs are free."""

    def __init__(self, api_key: str, cache_dir: Path = DEFAULT_CACHE, per_minute: int = 280):
        self.api_key = api_key
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = 60.0 / per_minute
        self._last = 0.0
        self.client = httpx.Client(timeout=30.0)

    def _path(self, endpoint: str, params: dict) -> Path:
        key = endpoint.strip("/").replace("/", "_") + "__" + "_".join(
            f"{k}-{params[k]}" for k in sorted(params)
        )
        return self.cache_dir / f"{key.replace(',', '+')}.json.gz"

    def get(self, endpoint: str, refresh: bool = False, **params):
        path = self._path(endpoint, params)
        if path.exists() and not refresh:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        qs = "&".join(f"{k}={quote(str(v), safe=',')}" for k, v in params.items())
        url = f"{BASE_URL}{endpoint}?{qs}{'&' if qs else ''}apikey={self.api_key}"
        for attempt in range(5):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.client.get(url)
            except httpx.HTTPError as e:
                logger.warning("network error %s (%s), retry %d", endpoint, e, attempt)
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                time.sleep(2 ** attempt * 5)
                continue
            if r.status_code != 200:
                return {"__error__": r.status_code, "__body__": r.text[:300].replace(self.api_key, "***")}
            data = r.json()
            with gzip.open(path, "wt", encoding="utf-8") as f:
                json.dump(data, f)
            return data
        return {"__error__": "retries exhausted"}

    # -- endpoints --------------------------------------------------------

    def bars(self, sym: str, start: date, end: date) -> list[dict]:
        """Dividend-adjusted daily bars (total-return-ish), oldest first.
        Falls back to the split-adjusted /full series."""
        data = self.get("/stable/historical-price-eod/dividend-adjusted", symbol=sym, **{"from": start.isoformat(), "to": end.isoformat()})
        rows = [
            {
                "date": r.get("date"),
                "open": r.get("adjOpen", r.get("open")),
                "high": r.get("adjHigh", r.get("high")),
                "low": r.get("adjLow", r.get("low")),
                "close": r.get("adjClose", r.get("close")),
                "volume": r.get("volume", 0),
            }
            for r in _rows(data)
        ]
        # Use the adjusted series only if it carries full OHLC; a close-only
        # payload would be dropped row by row later and silently empty the
        # dataset.
        if rows and all(r[k] is not None for r in rows[:20] for k in ("date", "open", "high", "low", "close")):
            return sorted(rows, key=lambda r: r["date"])
        data = self.get("/stable/historical-price-eod/full", symbol=sym, **{"from": start.isoformat(), "to": end.isoformat()})
        return sorted(
            ({k: r.get(k) for k in ("date", "open", "high", "low", "close", "volume")} for r in _rows(data)),
            key=lambda r: r["date"],
        )

    def raw_bars(self, sym: str, start: date, end: date) -> list[dict]:
        """Unadjusted (as-traded) close and volume. Only the dollar
        thresholds use these; returns come from the adjusted ``bars``."""
        data = self.get("/stable/historical-price-eod/non-split-adjusted", symbol=sym, **{"from": start.isoformat(), "to": end.isoformat()})
        # This endpoint has shipped its unadjusted values under adj* names.
        return [
            {"date": r.get("date"), "raw_close": r.get("close", r.get("adjClose")), "raw_volume": r.get("volume")}
            for r in _rows(data)
        ]

    def market_caps(self, sym: str, start: date, end: date) -> list[dict]:
        data = self.get("/stable/historical-market-capitalization", symbol=sym, limit=5000, **{"from": start.isoformat(), "to": end.isoformat()})
        return [{"date": r.get("date"), "market_cap": r.get("marketCap")} for r in _rows(data)]

    def statements(self, kind: str, sym: str, limit: int = 60) -> list[dict]:
        return _rows(self.get(f"/stable/{kind}", symbol=sym, period="quarter", limit=limit))

    def earnings(self, sym: str, limit: int = 120) -> list[dict]:
        return _rows(self.get("/stable/earnings", symbol=sym, limit=limit))

    def sector(self, sym: str) -> Optional[str]:
        rows = _rows(self.get("/stable/profile", symbol=sym))
        return rows[0].get("sector") if rows else None


def _rows(data) -> list[dict]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict) and isinstance(data.get("historical"), list):
        return data["historical"]
    return []


PLAN_CHECKS = [
    ("current S&P 500", "/stable/sp500-constituent", {}),
    ("historical S&P 500 changes", "/stable/historical-sp500-constituent", {}),
    ("dividend-adjusted bars", "/stable/historical-price-eod/dividend-adjusted", {"symbol": "AAPL", "from": "2015-01-01", "to": "2015-02-01"}),
    ("split-adjusted bars", "/stable/historical-price-eod/full", {"symbol": "AAPL", "from": "2015-01-01", "to": "2015-02-01"}),
    # AAPL traded ~$105-115 in Jan 2015; split-adjusted it is ~$26-29.
    ("unadjusted bars (expect AAPL ~$110)", "/stable/historical-price-eod/non-split-adjusted", {"symbol": "AAPL", "from": "2015-01-01", "to": "2015-02-01"}),
    ("historical market cap (from 2014)", "/stable/historical-market-capitalization", {"symbol": "AAPL", "from": "2014-01-01", "to": "2026-01-01", "limit": 5000}),
    ("income statement (quarterly, 60)", "/stable/income-statement", {"symbol": "AAPL", "period": "quarter", "limit": 60}),
    ("balance sheet (quarterly, 60)", "/stable/balance-sheet-statement", {"symbol": "AAPL", "period": "quarter", "limit": 60}),
    ("cash flow (quarterly, 60)", "/stable/cash-flow-statement", {"symbol": "AAPL", "period": "quarter", "limit": 60}),
    ("earnings history", "/stable/earnings", {"symbol": "AAPL", "limit": 120}),
    ("financial scores", "/stable/financial-scores", {"symbol": "AAPL"}),
    ("profile", "/stable/profile", {"symbol": "AAPL"}),
]


def check_plan(fetcher: FMPFetcher) -> list[dict]:
    """Hit each endpoint once (uncached) and summarise what came back."""
    out = []
    for label, ep, params in PLAN_CHECKS:
        data = fetcher.get(ep, refresh=True, **params)
        rows = _rows(data)
        if isinstance(data, dict) and "__error__" in data:
            status = f"HTTP {data['__error__']}"
        elif rows:
            status = f"ok ({len(rows)} rows)"
        else:
            status = "empty / unexpected shape"
        info = ""
        if rows:
            dates = [r.get("date") for r in rows if r.get("date")]
            if dates:
                info = f"{min(dates)} .. {max(dates)}"
            if ep == "/stable/earnings":
                info += f"; fields: {sorted(rows[0])}"
            if ep.startswith("/stable/historical-price-eod/"):
                r0 = rows[0]
                info += f"; close={r0.get('close', r0.get('adjClose'))}; fields: {sorted(r0)}"
            if ep == "/stable/income-statement":
                # Fallback market cap = adjusted price x these shares, which
                # is only right while FMP split-adjusts them (verified
                # 2026-10-05: AAPL 2011 = 26.3B; as filed it was ~0.9B).
                oldest = min(rows, key=lambda r: r.get("date") or "")
                info += f"; oldest weightedAverageShsOutDil={oldest.get('weightedAverageShsOutDil')}"
        out.append({"endpoint": label, "path": ep, "status": status, "info": info})
    return out


# ---------------------------------------------------------------------------
# Fetch + load
# ---------------------------------------------------------------------------


# Daily S&P 500 membership from github.com/fja05680/sp500 (MIT), pinned to one
# commit so the backtest universe can't drift between runs. Used when the FMP
# plan doesn't serve /stable/historical-sp500-constituent (ours: HTTP 402).
PUBLIC_MEMBERSHIP_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/a2430f2af0c79ddf0748e91de11bdeb1616ab5a7/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv"
)


def parse_public_membership(text: str, start: date) -> list[tuple[date, frozenset]]:
    """``date,tickers`` rows (members on and after each date) -> ascending
    ``(effective_date, members)`` from the last row on or before ``start``.
    Share-class dots become FMP's dashes (BRK.B -> BRK-B)."""
    rows = []
    for line in text.splitlines()[1:]:
        d, _, tickers = line.partition(",")
        day = _d(d)
        if day:
            syms = frozenset(t.strip().replace(".", "-") for t in tickers.strip().strip('"').split(",") if t.strip())
            rows.append((day, syms))
    rows.sort(key=lambda r: r[0])
    first = max((i for i, (day, _) in enumerate(rows) if day <= start), default=0)
    out = rows[first:]
    if out:
        out[0] = (date.min, out[0][1])
    return out


def public_membership(cache_dir: Path, start: date) -> list[tuple[date, frozenset]]:
    path = Path(cache_dir) / "sp500_membership_public.csv"
    if not path.exists():
        r = httpx.get(PUBLIC_MEMBERSHIP_URL, timeout=60.0, follow_redirects=True)
        r.raise_for_status()
        if not r.text.startswith("date,tickers"):
            raise RuntimeError("unexpected public membership file format")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(r.text, encoding="utf-8")
    return parse_public_membership(path.read_text(encoding="utf-8"), start)


def universe_symbols(fetcher: FMPFetcher, mode: str, start: date) -> tuple[list[tuple[date, frozenset]], bool]:
    if mode == "pit500public":
        return public_membership(fetcher.cache_dir, start), False
    if mode == "pit500":
        current = [r["symbol"] for r in _rows(fetcher.get("/stable/sp500-constituent")) if r.get("symbol")]
        events = _rows(fetcher.get("/stable/historical-sp500-constituent"))
        if not current or not events:
            raise RuntimeError("Point-in-time S&P 500 unavailable on this FMP plan; use --universe pit500public")
        return build_membership(current, events, start), False
    if mode == "current900":
        from app.services.universes import get_combined_universe

        return [(date.min, frozenset(get_combined_universe()))], True
    raise ValueError(mode)


# Same company, new ticker. The public membership file keeps the old symbol
# for dates before the rename, and FMP files some or all of that history
# under the new one (FB's statements live under META). Used only to fill
# pieces FMP returns empty for the old symbol. Fixed 2026-10-05 from the
# fetch's coverage gaps, before any backtest result existed.
RENAMES = {
    "FB": "META",     # Facebook -> Meta Platforms (2022)
    "TMK": "GL",      # Torchmark -> Globe Life (2019)
    "HRS": "LHX",     # Harris -> L3Harris (2019; Harris was the acquirer)
    "KORS": "CPRI",   # Michael Kors -> Capri Holdings (2018)
    "DWDP": "DD",     # DowDuPont -> DuPont de Nemours (2019)
    "CTL": "LUMN",    # CenturyLink -> Lumen (2020)
    "JEC": "J",       # Jacobs Engineering ticker change (2019)
}


def _with_alias(fn, sym: str, *args):
    """``fn(sym, ...)``, or the renamed symbol's data if that comes back empty."""
    got = fn(sym, *args)
    if not got and sym in RENAMES:
        got = fn(RENAMES[sym], *args)
    return got


def fetch_all(fetcher: FMPFetcher, mode: str, start: date, end: date) -> None:
    """Fill the cache for every symbol that was ever a member in [start, end]."""
    history_start = date(start.year - 2, 1, 1)  # 200-DMA + 8 quarters of runway
    membership, _ = universe_symbols(fetcher, mode, start)
    syms = sorted(set().union(*(m for _, m in membership)) | {"SPY"})
    syms += sorted({RENAMES[s] for s in syms if s in RENAMES} - set(syms))
    logger.info("fetching %d symbols", len(syms))
    for i, sym in enumerate(syms):
        fetcher.bars(sym, history_start, end)
        if sym != "SPY":
            fetcher.raw_bars(sym, history_start, end)
            fetcher.market_caps(sym, history_start, end)
            for kind in ("income-statement", "balance-sheet-statement", "cash-flow-statement"):
                fetcher.statements(kind, sym)
            fetcher.earnings(sym)
            fetcher.sector(sym)
        if i % 25 == 0:
            logger.info("  %d/%d %s", i, len(syms), sym)


def _frame(rows: list[dict]) -> Optional[pd.DataFrame]:
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).sort_values("date").reset_index(drop=True)
    df = df[df["close"] > 0]
    return df if len(df) else None


def attach_unadjusted(bars: pd.DataFrame, raw: list[dict], caps: list[dict]) -> pd.DataFrame:
    """Add raw_close / raw_volume (matched by date) and market_cap (last
    known value on or before each date, at most 10 days stale). A column with
    no data at all is left off, so the funnel falls back to adjusted close."""
    out = bars.copy()
    if raw:
        r = pd.DataFrame(raw)
        r["date"] = pd.to_datetime(r["date"], errors="coerce")
        for c in ("raw_close", "raw_volume"):
            r[c] = pd.to_numeric(r[c], errors="coerce")
        r = r.dropna(subset=["date"]).drop_duplicates("date").sort_values("date")
        out = out.merge(r, on="date", how="left")
    if caps:
        m = pd.DataFrame(caps)
        m["date"] = pd.to_datetime(m["date"], errors="coerce")
        m["market_cap"] = pd.to_numeric(m["market_cap"], errors="coerce")
        m = m.dropna().drop_duplicates("date").sort_values("date")
        if len(m):
            out = pd.merge_asof(out, m, on="date", direction="backward", tolerance=pd.Timedelta(days=10))
    for c in ("raw_close", "raw_volume", "market_cap"):
        if c in out.columns and not out[c].notna().any():
            out = out.drop(columns=c)
    return out.reset_index(drop=True)


def load_dataset(fetcher: FMPFetcher, mode: str, start: date, end: date) -> Dataset:
    history_start = date(start.year - 2, 1, 1)
    membership, biased = universe_symbols(fetcher, mode, start)
    syms = sorted(set().union(*(m for _, m in membership)))
    tickers, missing_bars, missing_fund, missing_raw, missing_cap = {}, [], [], [], []
    for sym in syms:
        bars = _frame(_with_alias(fetcher.bars, sym, history_start, end))
        if bars is None:
            missing_bars.append(sym)
            continue
        bars = attach_unadjusted(
            bars,
            _with_alias(fetcher.raw_bars, sym, history_start, end),
            _with_alias(fetcher.market_caps, sym, history_start, end),
        )
        if "raw_close" not in bars.columns:
            missing_raw.append(sym)
        if "market_cap" not in bars.columns:
            missing_cap.append(sym)
        statements = lambda kind: _with_alias(lambda s: fetcher.statements(kind, s), sym)  # noqa: E731
        inc = statements("income-statement")
        if not inc:
            missing_fund.append(sym)
        tickers[sym] = {
            "sector": _with_alias(fetcher.sector, sym),
            "bars": bars,
            "income": inc,
            "balance": statements("balance-sheet-statement"),
            "cashflow": statements("cash-flow-statement"),
            "earnings": _with_alias(fetcher.earnings, sym),
        }
    spy = _frame(fetcher.bars("SPY", history_start, end))
    if spy is None:
        raise RuntimeError("no SPY bars")
    return Dataset(
        tickers=tickers,
        spy=spy,
        membership=membership,
        universe_label={"pit500": "S&P 500, point-in-time (FMP)", "pit500public": "S&P 500, point-in-time (fja05680/sp500 @ a2430f2)"}.get(mode, "current S&P 500+400 (SURVIVORSHIP-BIASED)"),
        survivorship_biased=biased,
        coverage={
            "symbols_ever_member": len(syms),
            "with_bars": len(tickers),
            "missing_bars": missing_bars,
            "missing_fundamentals": missing_fund,
            "missing_unadjusted_prices": missing_raw,
            "missing_market_cap": missing_cap,
        },
    )


def synthetic_dataset(**kw) -> Dataset:
    from scripts.backtest.synthetic import make_market

    m = make_market(**kw)
    return Dataset(
        tickers=m["tickers"],
        spy=m["spy"],
        membership=[(d, frozenset(s)) for d, s in m["membership"]],
        universe_label="SYNTHETIC random-walk market (pipeline check only, not evidence)",
        survivorship_biased=False,
        coverage={"symbols_ever_member": len(m["tickers"]), "with_bars": len(m["tickers"]), "missing_bars": [], "missing_fundamentals": []},
    )


def api_key_from_env() -> str:
    key = os.environ.get("FMP_API_KEY", "")
    if not key or key in ("x", "ci-placeholder-not-a-real-key"):
        raise SystemExit("FMP_API_KEY is not set to a real key.")
    return key
