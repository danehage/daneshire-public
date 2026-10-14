"""Screener-built scan universe (#118).

Rows are trimmed copies of real /stable/company-screener rows from
2026-10-13, so each rule is pinned to the case that motivated it. No
network: the resolver gets a fake client.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.services import screener_universe as su
from app.services.fmp_client import FMPClient, FMPRequestError, prune_dated_cache_files
from app.services.screener_universe import (
    SCREENER_QUERIES,
    SOURCE_CACHE,
    SOURCE_SCREENER,
    SOURCE_STALE_CACHE,
    SOURCE_STATIC,
    SOURCE_STATIC_FALLBACK,
    ResolvedUniverse,
    UniverseCache,
    build_screener_universe,
    peek_universe_size,
    resolve_universe,
)
from app.services.universes import SP500_FULL, get_combined_universe

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)


def row(symbol, name=None, *, exchange="NYSE", country="US", price=100.0, avg_volume=1_000_000, **extra):
    """A screener row; defaults make it a plain liquid US common stock
    ($100M/day)."""
    return {
        "symbol": symbol,
        "companyName": name or f"{symbol} Holdings, Inc.",
        "marketCap": 10_000_000_000,
        "sector": "Industrials",
        "industry": "Specialty Industrial Machinery",
        "price": price,
        "volume": avg_volume,
        "avgVolume": avg_volume,
        "exchange": exchange,
        "exchangeShortName": exchange,
        "country": country,
        "isEtf": False,
        "isFund": False,
        "isActivelyTrading": True,
        **extra,
    }


def build(*rows, members=()):
    return build_screener_universe(list(rows), index_members=members)


def dropped_by(b, rule):
    return b.excluded[rule]


# ---------------------------------------------------------------------------
# build_screener_universe — one test per rule
# ---------------------------------------------------------------------------


def test_plain_us_common_stock_is_kept():
    b = build(row("NVDA", "NVIDIA Corporation", exchange="NASDAQ"), row("CAT", "Caterpillar Inc."))
    assert b.tickers == ["CAT", "NVDA"]
    assert b.raw_rows == 2
    assert sum(b.counts().values()) == 0


def test_only_major_us_exchanges():
    b = build(
        row("FNMA", "Federal National Mortgage Association", exchange="OTC"),
        row("UEC", "Uranium Energy Corp.", exchange="AMEX"),
        # Cboe Global Markets lists on its own exchange; an S&P 500 member.
        row("CBOE", "Cboe Global Markets, Inc.", exchange="CBOE"),
        row("XYZ", exchange="LSE"),
    )
    assert b.tickers == ["CBOE", "UEC"]
    assert dropped_by(b, "exchange") == ["FNMA", "XYZ"]


def test_etf_fund_inactive_and_small_rows_are_dropped_even_if_fmp_returns_them():
    b = build(
        row("ETF1", isEtf=True),
        row("FND", isFund=True),
        row("GONE", isActivelyTrading=False),
        row("TINY", marketCap=1_500_000_000),
        row("OK"),
    )
    assert b.tickers == ["OK"]
    assert dropped_by(b, "not_us_common") == ["ETF1", "FND", "GONE", "TINY"]


def test_foreign_domicile_dropped_unless_an_index_member():
    b = build(
        row("ACN", "Accenture plc", country="IE"),
        row("LIN", "Linde plc", exchange="NASDAQ", country="GB"),
        row("NIO", "NIO Inc.", country="CN"),
        row("TAK", "Takeda Pharmaceutical Company Limited", country="JP"),
        members=("ACN", "LIN"),
    )
    assert b.tickers == ["ACN", "LIN"]
    assert dropped_by(b, "foreign") == ["NIO", "TAK"]


def test_index_members_match_across_dot_and_dash_spelling():
    # The static lists spell classes BRK.B; FMP rows say BRK-B.
    b = build(row("XYZ-B", "Foreign Class B Co", country="BM"), members=("XYZ.B",))
    assert b.tickers == ["XYZ-B"]


def test_default_index_members_are_the_static_sp500_and_400():
    member = next(t for t in SP500_FULL if "." not in t)
    b = build_screener_universe([row(member, "Some plc", country="IE")])
    assert b.tickers == [member]


def test_preferred_warrant_unit_and_right_symbols_are_dropped():
    b = build(
        row("MER-PK", "Bank of America Corp", price=23.14, avg_volume=900_000),
        row("FITB-PA", "Fifth Third Bancorp", price=22.23, avg_volume=900_000),
        row("ABC-WS"),
        row("ABC-U"),
        row("ABC-RT"),
        row("ODD^X"),
        # Dashed share classes are common stock, not preferreds.
        row("BRK-B", "Berkshire Hathaway Inc."),
        row("BF-B", "Brown-Forman Corporation"),
        row("MOG-A", "Moog Inc."),
    )
    assert b.tickers == ["BF-B", "BRK-B", "MOG-A"]
    assert dropped_by(b, "non_common_symbol") == ["ABC-RT", "ABC-U", "ABC-WS", "FITB-PA", "MER-PK", "ODD^X"]


@pytest.mark.parametrize(
    "symbol,name",
    [
        ("TBB", "AT&T Inc. 5.35% GLB NTS 66"),
        ("DTW", "DTE Energy Company 5.25 % Debentures 2017-01.12.77 Global"),
        ("PRH", "Prudential Financial, Inc. 5.95"),
        ("CMSA", "CMS Energy Corporation 5.6% JRSUB NT 78"),
        ("FMCCT", "Freddie Mac 6.42% Non-Cum. Perp. Pfd."),
        ("XELLL", "Xcel Energy Inc. 6.25% Junior Subordinated Notes, Series due 2085"),
        ("SATA", "Strive Inc. Perp. Pfd. Series A"),
        ("ABXL", "Abacus Global Management, Inc. - 9.875% Fixed Rate Senior Notes due 2028"),
        ("SOJC", "The Southern Company JR 2017B NT 77"),
        ("WRNT", "Example Corp Warrants"),
    ],
)
def test_debt_and_preferred_names_are_dropped(symbol, name):
    b = build(row(symbol, name))
    assert dropped_by(b, "non_common_name") == [symbol]


@pytest.mark.parametrize(
    "name",
    [
        "Digital Realty Trust, Inc.",  # "Trust" is a REIT, not a note
        "Brookdale Senior Living Inc.",  # "Senior" alone is fine
        "Cummins Inc.",  # not "Cum."
        "PJT Partners Inc.",
        "Northern Trust Corporation",
        "United Rentals, Inc.",
    ],
)
def test_ordinary_company_names_are_not_mistaken_for_debt(name):
    assert build(row("KEEP", name)).tickers == ["KEEP"]


def test_k1_partnerships_are_dropped_but_lp_named_corporations_stay():
    b = build(
        row("EPD", "Enterprise Products Partners L.P."),
        row("ET", "Energy Transfer LP"),
        row("MPLX", "MPLX Lp"),
        row("WES", "Western Midstream Partners, LP"),
        # Elected corporate taxation; issue 1099s.
        row("HESM", "Hess Midstream LP"),
        row("PAGP", "Plains GP Holdings LP", exchange="NASDAQ"),
        # LLCs are not K-1s by name alone (LandBridge is a corporation).
        row("LB", "LandBridge Company LLC"),
    )
    assert b.tickers == ["HESM", "LB", "PAGP"]
    assert dropped_by(b, "partnership") == ["EPD", "ET", "MPLX", "WES"]


def test_illiquid_rows_are_dropped():
    b = build(
        # $10M/day is the floor (half the quality gate's $20M).
        row("EDGE", price=20.0, avg_volume=500_000),
        row("THIN", price=20.0, avg_volume=499_999),
        row("SOJE", "Southern Company (The) Series 2", price=20.0, avg_volume=59_000),
        row("NOPX", price=None),
    )
    assert b.tickers == ["EDGE"]
    assert dropped_by(b, "illiquid") == ["NOPX", "SOJE", "THIN"]


def test_share_classes_collapse_to_the_most_liquid_line():
    b = build(
        row("BRK-B", "Berkshire Hathaway Inc.", price=521.64, avg_volume=4_340_092),
        row("BRK-A", "Berkshire Hathaway Inc.", price=781_965.0, avg_volume=181),
        row("FOX", "Fox Corporation", exchange="NASDAQ", price=56.15, avg_volume=600_000),
        row("FOXA", "Fox Corporation", exchange="NASDAQ", price=62.87, avg_volume=3_000_000),
        row("Z", "Zillow Group, Inc. Class C", exchange="NASDAQ", price=27.86, avg_volume=3_900_000),
        row("ZG", "Zillow Group, Inc. Class A", exchange="NASDAQ", price=28.71, avg_volume=1_100_000),
    )
    assert b.tickers == ["BRK-B", "FOXA", "Z"]
    assert b.collapsed == {"BRK-B": ["BRK-A"], "FOXA": ["FOX"], "Z": ["ZG"]}
    assert dropped_by(b, "share_class") == ["BRK-A", "FOX", "ZG"]


def test_distinct_companies_with_similar_names_do_not_collapse():
    b = build(
        # Asset manager vs its BDC: legal suffix differs.
        row("OWL", "Blue Owl Capital Inc."),
        row("OBDC", "Blue Owl Capital Corporation"),
        # Puerto Rico bank (NYSE) vs North Carolina bank (NASDAQ).
        row("FBP", "First BanCorp."),
        row("FBNC", "First Bancorp", exchange="NASDAQ"),
    )
    assert b.tickers == ["FBNC", "FBP", "OBDC", "OWL"]
    assert b.collapsed == {}


def test_rows_repeated_across_both_queries_count_once():
    us = [row("AAA"), row("BBB")]
    every_domicile = [row("AAA"), row("BBB"), row("CCC", country="IE")]
    b = build_screener_universe(us + every_domicile, index_members=("CCC",))
    assert b.tickers == ["AAA", "BBB", "CCC"]
    assert b.raw_rows == 3


def test_malformed_rows_are_ignored():
    b = build_screener_universe([None, {}, {"symbol": ""}, "AAPL", row("OK")], index_members=())
    assert b.tickers == ["OK"]


def test_two_queries_one_us_one_every_domicile():
    us, every = SCREENER_QUERIES
    assert us["country"] == "US"
    assert "country" not in every
    for q in SCREENER_QUERIES:
        assert q["marketCapMoreThan"] == 2_000_000_000
        assert q["isEtf"] == q["isFund"] == "false"
        assert q["isActivelyTrading"] == "true"


# ---------------------------------------------------------------------------
# UniverseCache
# ---------------------------------------------------------------------------


def _cache(tmp_path) -> UniverseCache:
    return UniverseCache(str(tmp_path / "scanner_cache" / "universe_screener.json"))


def test_cache_round_trip(tmp_path):
    cache = _cache(tmp_path)
    assert cache.load() is None
    cache.save(build(row("AAA"), row("BBB")), NOW)
    loaded = cache.load()
    assert loaded.tickers == ["AAA", "BBB"]
    assert loaded.fetched_at == NOW
    assert loaded.counts["raw_rows"] == 2


def test_corrupt_cache_reads_as_missing(tmp_path):
    cache = _cache(tmp_path)
    os.makedirs(os.path.dirname(cache.path))
    with open(cache.path, "w") as f:
        f.write("{not json")
    assert cache.load() is None
    with open(cache.path, "w") as f:
        json.dump({"tickers": [], "fetched_at": NOW.isoformat()}, f)
    assert cache.load() is None


def test_cache_file_survives_the_dated_cache_prune(tmp_path):
    cache = _cache(tmp_path)
    cache.save(build(row("AAA")), NOW - timedelta(days=30))
    prune_dated_cache_files(os.path.dirname(cache.path))
    assert cache.load() is not None


# ---------------------------------------------------------------------------
# resolve_universe
# ---------------------------------------------------------------------------


class FakeScreenerClient:
    def __init__(self, rows=None, fail=False):
        self.rows = rows or []
        self.fail = fail
        self.calls: list[dict] = []

    async def get_company_screener(self, params):
        self.calls.append(params)
        if self.fail:
            raise RuntimeError("HTTP 503")
        return list(self.rows)


@pytest.fixture
def small_ok(monkeypatch):
    # Real builds are ~1,600 names; tests use a handful.
    monkeypatch.setattr(su, "MIN_UNIVERSE_SIZE", 1)


async def test_static_names_need_no_client():
    r = await resolve_universe("sp500", client=None)
    assert r.source == SOURCE_STATIC
    assert r.tickers == SP500_FULL
    assert r.label == "sp500"


async def test_unknown_name_resolves_to_none():
    assert await resolve_universe("robinhood", client=None) is None


async def test_screener_build_is_made_cached_and_reused(tmp_path, small_ok):
    cache = _cache(tmp_path)
    client = FakeScreenerClient([row("AAA"), row("BBB")])

    first = await resolve_universe("combined", client, cache, now=NOW)
    assert first.source == SOURCE_SCREENER
    assert first.tickers == ["AAA", "BBB"]
    assert first.label == "combined_screener"
    assert client.calls == list(SCREENER_QUERIES)

    client.calls.clear()
    later = await resolve_universe("combined", client, cache, now=NOW + timedelta(days=6))
    assert later.source == SOURCE_CACHE
    assert later.tickers == ["AAA", "BBB"]
    assert client.calls == []


async def test_week_old_build_is_rebuilt(tmp_path, small_ok):
    cache = _cache(tmp_path)
    cache.save(build(row("OLD")), NOW - timedelta(days=7))
    client = FakeScreenerClient([row("NEW")])
    r = await resolve_universe("combined", client, cache, now=NOW)
    assert r.source == SOURCE_SCREENER
    assert r.tickers == ["NEW"]
    assert cache.load().tickers == ["NEW"]


async def test_screener_failure_falls_back_to_stale_build(tmp_path, small_ok):
    cache = _cache(tmp_path)
    cache.save(build(row("OLD")), NOW - timedelta(days=20))
    r = await resolve_universe("combined", FakeScreenerClient(fail=True), cache, now=NOW)
    assert r.source == SOURCE_STALE_CACHE
    assert r.tickers == ["OLD"]
    assert r.label == "combined_screener"


async def test_screener_failure_without_a_build_falls_back_to_static(tmp_path, small_ok):
    cache = _cache(tmp_path)
    r = await resolve_universe("combined", FakeScreenerClient(fail=True), cache, now=NOW)
    assert r.source == SOURCE_STATIC_FALLBACK
    assert r.tickers == get_combined_universe()
    # Same label as pre-#118 scans of the static list.
    assert r.label == "combined"
    assert cache.load() is None


async def test_no_client_falls_back_to_static(tmp_path):
    r = await resolve_universe("combined", None, _cache(tmp_path), now=NOW)
    assert r.source == SOURCE_STATIC_FALLBACK


async def test_suspiciously_small_build_is_not_used(tmp_path):
    # MIN_UNIVERSE_SIZE left at its real value: two names is a broken answer.
    cache = _cache(tmp_path)
    r = await resolve_universe("combined", FakeScreenerClient([row("AAA"), row("BBB")]), cache, now=NOW)
    assert r.source == SOURCE_STATIC_FALLBACK
    assert cache.load() is None


def test_peek_never_needs_a_client(tmp_path):
    cache = _cache(tmp_path)
    assert peek_universe_size("combined", cache) is None
    assert peek_universe_size("sp500", cache) == len(SP500_FULL)
    assert peek_universe_size("robinhood", cache) is None
    cache.save(build(row("AAA"), row("BBB")), NOW)
    assert peek_universe_size("combined", cache) == 2


def test_label_marks_screener_sources():
    assert ResolvedUniverse("combined", ["A"], SOURCE_CACHE).label == "combined_screener"
    assert ResolvedUniverse("combined", ["A"], SOURCE_STATIC_FALLBACK).label == "combined"


# ---------------------------------------------------------------------------
# FMPClient.get_company_screener
# ---------------------------------------------------------------------------


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


async def test_client_screener_builds_query_and_returns_rows(tmp_path):
    client = FMPClient(api_key="test-key", cache_dir=str(tmp_path / "fmp"))
    urls = []

    async def fake_request(url, http_client, retries=None):
        urls.append(url)
        return _Resp([row("AAA"), "junk"])

    client._request_with_retry = fake_request
    rows = await client.get_company_screener(SCREENER_QUERIES[0])
    assert [r["symbol"] for r in rows] == ["AAA"]
    assert "/stable/company-screener?" in urls[0]
    assert "marketCapMoreThan=2000000000" in urls[0]
    assert "country=US" in urls[0]


async def test_client_screener_raises_on_failure_or_bad_payload(tmp_path):
    client = FMPClient(api_key="test-key", cache_dir=str(tmp_path / "fmp"))

    async def no_response(url, http_client, retries=None):
        return None

    client._request_with_retry = no_response
    with pytest.raises(FMPRequestError):
        await client.get_company_screener({"limit": 1})

    async def error_payload(url, http_client, retries=None):
        return _Resp({"Error Message": "Limit Reach"})

    client._request_with_retry = error_payload
    with pytest.raises(FMPRequestError):
        await client.get_company_screener({"limit": 1})
