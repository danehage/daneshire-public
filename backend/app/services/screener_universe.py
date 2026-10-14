"""
Scan universe built from FMP's company screener (#118).

Every liquid US-listed operating company at or above $2B market cap, instead
of the hand-maintained S&P 500 + 400 list. Four parts:

* ``build_screener_universe`` — a pure function over the screener rows. All
  the cleanup rules live here, each one named so the build can report what
  it dropped and why.
* ``fetch_screener_rows`` — the two FMP calls (see ``SCREENER_QUERIES``).
* ``UniverseCache`` — a JSON file next to the Stage-1 scan cache, refreshed
  weekly. Not date-suffixed, so ``prune_dated_cache_files`` leaves it alone.
* ``resolve_universe`` — what the scan route calls. ``combined`` comes from
  a fresh cache, else new screener calls, else the last cache of any age,
  else the static S&P 500 + 400 list; the source used is logged and
  returned.

The rules were derived from the screener's real output on 2026-10-13
(1,689 US-domicile rows + 2,080 all-domicile rows; the #118 PR has the
per-rule counts).
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from app.services.universes import STATIC_UNIVERSES, get_combined_universe

logger = logging.getLogger(__name__)

# Queries sent to /stable/company-screener; ``limit`` is far above what
# either returns, so results are never silently truncated.
#
# Two calls because FMP's ``country`` is the company's domicile, not its
# listing. ``country=US`` alone drops 34 current S&P 500/400 members that
# are US-listed and file 10-Qs like any US company but are domiciled
# elsewhere (ACN, LIN, MDT, ETN, CB, TT, JCI, NXPI, LULU, ... on
# 2026-10-13). The second, country-less query returns NYSE/NASDAQ/AMEX
# listings of every domicile; its non-US rows are kept only for those
# index members (``foreign`` rule). Other foreign listings are out of
# scope (#118): many report semiannually and the gate's TTM math fails
# them. The country-less query returns no OTC or CBOE rows, so it can't
# replace the first one (Cboe Global Markets is a US company on CBOE).
_BASE_QUERY = {
    "marketCapMoreThan": 2_000_000_000,
    "isEtf": "false",
    "isFund": "false",
    "isActivelyTrading": "true",
    "limit": 10_000,
}
SCREENER_QUERIES = ({**_BASE_QUERY, "country": "US"}, dict(_BASE_QUERY))

MIN_MARKET_CAP = 2_000_000_000

# NYSE, NASDAQ and NYSE American (FMP: AMEX). CBOE is here because
# Cboe Global Markets (CBOE, an S&P 500 member) lists on its own exchange;
# it was the only CBOE row on 2026-10-13. Everything else the screener
# returns is OTC (Fannie/Freddie, utility preferreds, foreign ordinaries).
ALLOWED_EXCHANGES = frozenset({"NYSE", "NASDAQ", "AMEX", "CBOE"})

# avgVolume x price. Half the quality gate's $20M/day (FunnelConfig
# .min_dollar_volume, judged on 20-day bars at scan time), so names near
# the gate's line stay in the universe and the gate decides. Below this
# the screener rows are almost all exchange-traded notes and preferreds
# that the name rules can't see (e.g. "Southern Company (The) Series 2",
# "PennantPark Floating Rate Capital Ltd." = PFLA notes). No S&P 500/400
# member was under it on 2026-10-13.
MIN_AVG_DOLLAR_VOLUME = 10_000_000

# A build smaller than this means the screener answered with something
# partial or wrong; treat it as a failed call rather than scan a sliver.
MIN_UNIVERSE_SIZE = 500

# Symbol shapes FMP uses for non-common lines: BAC-PK / FITB-PA (preferred),
# -W / -WS / -WT (warrants), -U / -UN (units), -R / -RT (rights).
# Dashed share classes (BRK-B, BF-B, MOG-A, MKC-V) are *not* matched here —
# they are common stock and go through the share-class collapse instead.
_NON_COMMON_SYMBOL = re.compile(r"-(P[A-Z]?|W[ST]?|UN?|RT?)$")
# Anything else that isn't a plain 1-5 letter ticker with an optional
# 1-2 letter class suffix.
_PLAIN_SYMBOL = re.compile(r"^[A-Z]{1,5}(-[A-Z]{1,2})?$")

# Company-name markers of exchange-traded debt, preferreds, units, warrants
# and rights. Baby bonds often carry plain tickers (TBB, SOJE, DUKB, UNMA),
# so the name is the only tell. Each alternative was seen in the 2026-10-13
# data, e.g.:
#   "AT&T Inc. 5.35% GLB NTS 66"              coupon %, NTS
#   "Prudential Financial, Inc. 5.95"          bare trailing coupon
#   "CMS Energy Corporation 5.6% JRSUB NT 78"  JRSUB, NT
#   "Freddie Mac 6.42% Non-Cum. Perp. Pfd."    Cum, Perp, Pfd
#   "... Junior Subordinated Notes due 2060"   Subordinated, Notes
# Deliberately absent: "Trust" (REITs), "Senior" alone (Brookdale Senior
# Living), "Partners" (PJT Partners Inc.), "Series" (DTE's "2020 Series"
# notes are caught by the liquidity floor instead).
_NON_COMMON_NAME = re.compile(
    r"\d\s*%"
    r"|\b\d{1,2}\.\d{2,4}\s*$"
    r"|\b(notes?|nts?|nt|debentures?|pfd|preferred|perp|perpetual|cum"
    r"|depositary|subordinated?|subordinat|jrsub|warrants?|rights|units)\b"
    r"|\bsenior\s+notes?\b|\bfixed\s+rate\b",
    re.IGNORECASE,
)

# Publicly traded partnerships issue K-1s, not 1099s. The marker is the
# legal name ending in "LP" / "L.P." (EPD, ET, MPLX "MPLX Lp", CQP, WES,
# PAA, SUN, ARLP, AB, BSM, DKL, IEP on 2026-10-13). Names with "Partners"
# that don't end in LP are corporations (PJT Partners Inc.) and stay.
_PARTNERSHIP_NAME = re.compile(r"\bl\.?\s?p\.?$", re.IGNORECASE)
# LP-named entities that elected corporate taxation and issue 1099s.
LP_NAMED_CORPORATIONS = frozenset({"HESM", "PAGP"})

# Share-class collapse key: the company name minus class labels, case and
# punctuation ("Zillow Group, Inc. Class A" == "... Class C"), plus the
# exchange. Legal suffixes are kept and the exchange is part of the key
# because distinct companies share names once punctuation goes: "Blue Owl
# Capital Inc." (OWL) vs "Blue Owl Capital Corporation" (OBDC, a BDC), and
# "First BanCorp." (FBP, NYSE) vs "First Bancorp" (FBNC, NASDAQ). Every real
# share-class pair on 2026-10-13 (BRK-A/B, FOX/FOXA, NWS/NWSA, UA/UAA,
# Z/ZG) shares an exchange.
_CLASS_LABEL = re.compile(
    r"\b(class|series)\s+[a-z]\b(\s+common\s+stock)?|\bcommon\s+stock\b"
)

# Rule names, in the order they are applied.
RULES = (
    "exchange",
    "not_us_common",
    "foreign",
    "non_common_symbol",
    "non_common_name",
    "partnership",
    "illiquid",
    "share_class",
)


@dataclass
class UniverseBuild:
    """Result of ``build_screener_universe``."""

    tickers: list[str]
    raw_rows: int
    # rule -> symbols it dropped (FMP spelling), in RULES order
    excluded: dict[str, list[str]] = field(default_factory=dict)
    # kept symbol -> the other share classes collapsed into it
    collapsed: dict[str, list[str]] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {rule: len(self.excluded.get(rule, [])) for rule in RULES}


def _num(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f else 0.0


def avg_dollar_volume(row: dict) -> float:
    return _num(row.get("avgVolume")) * _num(row.get("price"))


def company_key(row: dict) -> tuple[str, str]:
    """(normalised name, exchange); share classes of one company share it."""
    s = (row.get("companyName") or row.get("symbol") or "").lower()
    s = _CLASS_LABEL.sub(" ", s)
    s = re.sub(r"[^a-z0-9&]+", " ", s)
    return " ".join(s.split()), row.get("exchangeShortName") or ""


def _dashed(symbol: str) -> str:
    """FMP's share-class spelling. Universe tickers keep it (BRK-B, MOG-A):
    on our plan /stable/quote, historical-price-eod and the statements
    answer 402 for the dotted BRK.B / MOG.A the static lists use, and serve
    the dashed form (checked 2026-10-13)."""
    return symbol.replace(".", "-")


def _exclusion_rule(row: dict, index_members: frozenset) -> Optional[str]:
    symbol = row.get("symbol") or ""
    name = row.get("companyName") or ""
    if row.get("exchangeShortName") not in ALLOWED_EXCHANGES:
        return "exchange"
    if (
        row.get("isEtf")
        or row.get("isFund")
        or row.get("isActivelyTrading") is False
        or _num(row.get("marketCap")) < MIN_MARKET_CAP
    ):
        # The query already asks for these; checked again so a changed
        # FMP default can't widen the universe silently.
        return "not_us_common"
    if (row.get("country") or "US") != "US" and symbol not in index_members:
        return "foreign"
    if _NON_COMMON_SYMBOL.search(symbol) or not _PLAIN_SYMBOL.match(symbol):
        return "non_common_symbol"
    if _NON_COMMON_NAME.search(name):
        return "non_common_name"
    if _PARTNERSHIP_NAME.search(name.strip().rstrip(",")) and symbol not in LP_NAMED_CORPORATIONS:
        return "partnership"
    if avg_dollar_volume(row) < MIN_AVG_DOLLAR_VOLUME:
        return "illiquid"
    return None


def build_screener_universe(
    rows: Iterable[dict], index_members: Optional[Iterable[str]] = None
) -> UniverseBuild:
    """Clean screener rows into a scan universe. Pure; no I/O.

    Rules, in order (first match wins): exchange, not_us_common, foreign
    (non-US domicile unless in ``index_members``, default the static S&P
    500 + 400), non_common_symbol, non_common_name, partnership, illiquid;
    then share classes of one company collapse to the most liquid line
    (avgVolume x price). A symbol returned by both queries counts once.
    Returns sorted scan symbols plus what each rule dropped.
    """
    members = frozenset(
        _dashed(t) for t in (get_combined_universe() if index_members is None else index_members)
    )
    excluded: dict[str, list[str]] = {rule: [] for rule in RULES}
    kept: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("symbol"):
            continue
        symbol = row["symbol"]
        if symbol in seen:
            continue
        seen.add(symbol)
        rule = _exclusion_rule(row, members)
        if rule:
            excluded[rule].append(symbol)
        else:
            kept.append(row)

    by_company: dict[tuple[str, str], list[dict]] = {}
    for row in kept:
        by_company.setdefault(company_key(row), []).append(row)

    tickers: list[str] = []
    collapsed: dict[str, list[str]] = {}
    for group in by_company.values():
        # Most liquid first; symbol breaks ties so the build is deterministic.
        group.sort(key=lambda r: (-avg_dollar_volume(r), r["symbol"]))
        winner, *rest = group
        tickers.append(winner["symbol"])
        if rest:
            dropped = [r["symbol"] for r in rest]
            collapsed[winner["symbol"]] = dropped
            excluded["share_class"].extend(dropped)

    return UniverseBuild(
        tickers=sorted(tickers),
        raw_rows=len(seen),
        excluded={rule: sorted(syms) for rule, syms in excluded.items()},
        collapsed=collapsed,
    )


async def fetch_screener_rows(client) -> list[dict]:
    """Both ``SCREENER_QUERIES``, concatenated. Raises if either fails: a
    half universe (no foreign-domiciled index members) is worse than last
    week's."""
    rows: list[dict] = []
    for params in SCREENER_QUERIES:
        rows.extend(await client.get_company_screener(params))
    return rows


# ---------------------------------------------------------------------------
# Weekly file cache
# ---------------------------------------------------------------------------

REFRESH_AFTER = timedelta(days=7)
DEFAULT_CACHE_PATH = os.path.join("scanner_cache", "universe_screener.json")


@dataclass
class CachedUniverse:
    tickers: list[str]
    fetched_at: datetime
    counts: dict[str, int] = field(default_factory=dict)

    def age(self, now: datetime) -> timedelta:
        return now - self.fetched_at


class UniverseCache:
    """The last screener build, as JSON on local disk.

    On Cloud Run the filesystem is per instance and in memory, so this
    lives as long as the instance (at most the week); a fresh instance
    makes the screener calls on its first scan. Small: ~1,600 symbols.
    """

    def __init__(self, path: str = DEFAULT_CACHE_PATH, max_age: timedelta = REFRESH_AFTER):
        self.path = path
        self.max_age = max_age

    def load(self) -> Optional[CachedUniverse]:
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
            tickers = [t for t in data["tickers"] if isinstance(t, str)]
            fetched_at = datetime.fromisoformat(data["fetched_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not tickers:
            return None
        return CachedUniverse(tickers, fetched_at, data.get("counts") or {})

    def save(self, build: UniverseBuild, now: datetime) -> None:
        payload = {
            "fetched_at": now.isoformat(),
            "tickers": build.tickers,
            "counts": {"raw_rows": build.raw_rows, **build.counts()},
        }
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning(f"Universe: could not write cache {self.path}: {e}")


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

SCREENER_UNIVERSE = "combined"

# Where a resolved universe came from.
SOURCE_STATIC = "static"  # a fixed list by design (sp500, sp400, ...)
SOURCE_CACHE = "screener_cache"  # cached build younger than a week
SOURCE_SCREENER = "screener"  # built from screener calls just now
SOURCE_STALE_CACHE = "screener_stale_cache"  # screener failed; older build
SOURCE_STATIC_FALLBACK = "static_fallback"  # screener failed, no build at all

SCREENER_SOURCES = frozenset({SOURCE_CACHE, SOURCE_SCREENER, SOURCE_STALE_CACHE})


@dataclass
class ResolvedUniverse:
    name: str
    tickers: list[str]
    source: str
    fetched_at: Optional[datetime] = None

    @property
    def label(self) -> str:
        """Name recorded with the scan (Stage-1 cache key, scan_picks).

        Screener builds are ``combined_screener`` so the forward record
        keeps them apart from pre-#118 ``combined`` rows (the static S&P
        500 + 400), which the static fallback still records as."""
        if self.source in SCREENER_SOURCES:
            return f"{self.name}_screener"
        return self.name


_resolve_lock = asyncio.Lock()


async def resolve_universe(
    name: str,
    client,
    cache: Optional[UniverseCache] = None,
    now: Optional[datetime] = None,
) -> Optional[ResolvedUniverse]:
    """Tickers for a universe name; None for an unknown name.

    ``combined``: fresh cache -> screener calls -> cache of any age ->
    static S&P 500 + 400. Every other name is its static list.
    """
    if name != SCREENER_UNIVERSE:
        tickers = STATIC_UNIVERSES.get(name)
        return ResolvedUniverse(name, list(tickers), SOURCE_STATIC) if tickers else None

    cache = cache or UniverseCache()
    now = now or datetime.now(timezone.utc)
    async with _resolve_lock:
        cached = cache.load()
        if cached and cached.age(now) < cache.max_age:
            logger.info(
                f"Universe {name}: {len(cached.tickers)} tickers from the "
                f"screener build of {cached.fetched_at:%Y-%m-%d}"
            )
            return ResolvedUniverse(name, cached.tickers, SOURCE_CACHE, cached.fetched_at)

        build = await _fetch_and_build(client)
        if build is not None:
            cache.save(build, now)
            logger.info(
                f"Universe {name}: built {len(build.tickers)} tickers from "
                f"{build.raw_rows} screener rows; dropped {build.counts()}"
            )
            return ResolvedUniverse(name, build.tickers, SOURCE_SCREENER, now)

        if cached:
            logger.warning(
                f"Universe {name}: screener unavailable; using the "
                f"{cached.age(now).days}-day-old build ({len(cached.tickers)} tickers)"
            )
            return ResolvedUniverse(name, cached.tickers, SOURCE_STALE_CACHE, cached.fetched_at)

        fallback = get_combined_universe()
        logger.warning(
            f"Universe {name}: screener unavailable and no cached build; "
            f"falling back to the static S&P 500 + 400 ({len(fallback)} tickers)"
        )
        return ResolvedUniverse(name, fallback, SOURCE_STATIC_FALLBACK)


async def _fetch_and_build(client) -> Optional[UniverseBuild]:
    if client is None:
        return None
    try:
        rows = await fetch_screener_rows(client)
    except Exception as e:  # noqa: BLE001 - any failure means "use a fallback"
        logger.warning(f"Universe: screener call failed: {e}")
        return None
    build = build_screener_universe(rows)
    if len(build.tickers) < MIN_UNIVERSE_SIZE:
        logger.warning(
            f"Universe: screener build has only {len(build.tickers)} tickers "
            f"from {build.raw_rows} rows (< {MIN_UNIVERSE_SIZE}); not using it"
        )
        return None
    return build


def peek_universe_size(name: str, cache: Optional[UniverseCache] = None) -> Optional[int]:
    """Size of a universe without any network call (for the universe
    picker, which guests can reach). None when ``combined`` has no build
    on this instance yet — the first scan makes one."""
    if name != SCREENER_UNIVERSE:
        tickers = STATIC_UNIVERSES.get(name)
        return len(tickers) if tickers else None
    cached = (cache or UniverseCache()).load()
    return len(cached.tickers) if cached else None
