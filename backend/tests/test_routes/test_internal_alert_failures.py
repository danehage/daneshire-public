"""
Loud job failures: a provider outage must not look like a successful run.

That the March-September 2026 401 outage went unnoticed showed how little
the alert jobs reported. An FMP outage (e.g. a revoked key) is quieter
still: every price/technical evaluation errors while the cron endpoints
kept answering 200 and logging nothing. These tests pin the fix:

- ``AlertEngine.run`` logs one summary line per run and still commits the
  alert_history rows;
- the internal cron endpoints answer 502 when every evaluation errored
  *and* the provider is to blame (UpstreamError / RateLimited), so Cloud
  Scheduler records a failure;
- a run whose only errors are the alerts' own (a delisted ticker) answers
  200 with a WARNING — it must not fail the job every 15 minutes;
- the manual ``POST /api/alerts/evaluate`` is unaffected.

DB-free: the engine runs against an in-memory session stand-in, so nothing
touches Postgres.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from app.config import settings
from app.main import app
from app.models.alert import Alert, AlertHistory
from app.routes.dependencies import get_alert_engine
from app.schemas.alert_runs import RunSummary
from app.schemas.market import PriceQuote
from app.services.alert_engine import AlertEngine
from app.services.market import RateLimited, TickerNotFound, UpstreamError

TEST_SECRET = "test-scheduler-secret"


@pytest.fixture(autouse=True)
def _scheduler_secret(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_secret", TEST_SECRET)


def _auth():
    return {"X-Scheduler-Secret": TEST_SECRET}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeSession:
    added: list = field(default_factory=list)
    commits: int = 0

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def execute(self, stmt):
        # The engine's claim/revert UPDATEs; report one row so the claim wins.
        return SimpleNamespace(rowcount=1)


@dataclass
class _FakeMarket:
    """Quotes by ticker; anything unlisted gets ``missing(ticker)``."""

    quotes_by_ticker: dict = field(default_factory=dict)
    missing: type = UpstreamError

    async def quotes(self, tickers):
        return {
            t: self.quotes_by_ticker.get(t, self.missing(t, "HTTP 401"))
            for t in tickers
        }


def _outage() -> _FakeMarket:
    return _FakeMarket(missing=UpstreamError)


def _delisted() -> _FakeMarket:
    return _FakeMarket(missing=TickerNotFound)


def _price_alert(ticker: str) -> Alert:
    return Alert(
        id=uuid.uuid4(),
        ticker=ticker,
        name=f"{ticker} above 1",
        alert_type="price_cross",
        condition={"metric": "price", "operator": ">", "value": 1},
        status="active",
        priority="normal",
    )


def _engine(alerts: list[Alert], market: _FakeMarket) -> tuple[AlertEngine, _FakeSession]:
    db = _FakeSession()

    async def notifier(alert, condition, outcome):
        return True

    engine = AlertEngine(db, market=market, notifier=notifier)  # type: ignore[arg-type]

    async def active(alert_type=None):
        return alerts

    engine.get_active_alerts = active  # type: ignore[method-assign]
    return engine, db


def _summary_records(caplog):
    return [
        r for r in caplog.records
        if r.name == "app.services.alert_engine" and "alert-engine" in r.getMessage()
    ]


# ---------------------------------------------------------------------------
# Engine: classification, summary log, history still committed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_outage_logs_error_and_still_writes_history(caplog):
    alerts = [_price_alert("AAPL"), _price_alert("MSFT")]
    engine, db = _engine(alerts, _outage())

    with caplog.at_level(logging.INFO, logger="app.services.alert_engine"):
        summary = await engine.run("price_cross")

    assert (summary.errored, summary.upstream_errored) == (2, 2)
    assert summary.upstream_outage
    # Every evaluation still lands in alert_history, and is committed.
    assert sum(isinstance(o, AlertHistory) for o in db.added) == 2
    assert db.commits == 1

    [record] = _summary_records(caplog)
    assert record.levelno == logging.ERROR
    assert record.getMessage() == (
        "alert-engine price_cross: active=2 met=0 not_met=0 errored=2 notified=0"
    )


@pytest.mark.asyncio
async def test_rate_limited_counts_as_upstream():
    engine, _ = _engine([_price_alert("AAPL")], _FakeMarket(missing=RateLimited))
    summary = await engine.run("price_cross")
    assert summary.upstream_outage


@pytest.mark.asyncio
async def test_delisted_only_run_logs_warning_not_outage(caplog):
    engine, db = _engine([_price_alert("GONE")], _delisted())

    with caplog.at_level(logging.INFO, logger="app.services.alert_engine"):
        summary = await engine.run("price_cross")

    assert (summary.errored, summary.upstream_errored) == (1, 0)
    assert summary.all_errored and not summary.upstream_outage
    assert db.commits == 1
    [record] = _summary_records(caplog)
    assert record.levelno == logging.WARNING


@pytest.mark.asyncio
async def test_bad_condition_is_not_upstream():
    alert = _price_alert("AAPL")
    alert.condition = {"metric": "price", "operator": "~~", "value": "x"}
    engine, _ = _engine(
        [alert], _FakeMarket(quotes_by_ticker={"AAPL": PriceQuote(ticker="AAPL", price=1.0)})
    )
    summary = await engine.run("price_cross")
    assert summary.all_errored and not summary.upstream_outage


@pytest.mark.asyncio
async def test_partial_errors_log_info(caplog):
    alerts = [_price_alert("AAPL"), _price_alert("MSFT")]
    market = _FakeMarket(quotes_by_ticker={"AAPL": PriceQuote(ticker="AAPL", price=150.0)})
    engine, _ = _engine(alerts, market)

    with caplog.at_level(logging.INFO, logger="app.services.alert_engine"):
        summary = await engine.run("price_cross")

    assert (summary.met, summary.errored) == (1, 1)
    assert not summary.upstream_outage
    [record] = _summary_records(caplog)
    assert record.levelno == logging.INFO
    assert "active=2 met=1 not_met=0 errored=1 notified=1" in record.getMessage()


@pytest.mark.asyncio
async def test_zero_alerts_still_logs_a_summary(caplog):
    engine, db = _engine([], _outage())

    with caplog.at_level(logging.INFO, logger="app.services.alert_engine"):
        summary = await engine.run("price_cross")

    assert not summary.all_errored
    assert db.commits == 0
    [record] = _summary_records(caplog)
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        "alert-engine price_cross: active=0 met=0 not_met=0 errored=0 notified=0"
    )


def test_upstream_errored_is_not_in_the_response_body():
    body = RunSummary(alert_type="x", errored=1, upstream_errored=1).model_dump()
    assert "upstream_errored" not in body


def test_merged_summary_keeps_upstream_count():
    merged = RunSummary(alert_type="a", errored=1, upstream_errored=1).merged_with(
        RunSummary(alert_type="b", errored=2)
    )
    assert merged.upstream_errored == 1


# ---------------------------------------------------------------------------
# Routes: real engine end to end (fake session + market)
# ---------------------------------------------------------------------------


@pytest.fixture
def wire_engine():
    def install(alerts: list[Alert], market: _FakeMarket):
        engine, _ = _engine(alerts, market)
        app.dependency_overrides[get_alert_engine] = lambda: engine

    yield install
    app.dependency_overrides.pop(get_alert_engine, None)


@pytest.mark.asyncio
async def test_price_checks_return_502_on_provider_outage(client: AsyncClient, wire_engine):
    wire_engine([_price_alert("AAPL"), _price_alert("MSFT")], _outage())

    response = await client.post("/api/internal/alerts/run-price-checks", headers=_auth())

    assert response.status_code == 502
    assert "market data provider unavailable" in response.json()["detail"]


@pytest.mark.asyncio
async def test_price_checks_return_200_and_warn_when_only_delisted(
    client: AsyncClient, wire_engine, caplog
):
    wire_engine([_price_alert("GONE")], _delisted())

    with caplog.at_level(logging.INFO, logger="app.services.alert_engine"):
        response = await client.post(
            "/api/internal/alerts/run-price-checks", headers=_auth()
        )

    assert response.status_code == 200
    assert response.json()["errored"] == 1
    assert "upstream_errored" not in response.json()
    [record] = _summary_records(caplog)
    assert record.levelno == logging.WARNING


# ---------------------------------------------------------------------------
# Routes: every cron endpoint applies the rule; manual evaluate unaffected
# ---------------------------------------------------------------------------


class _StubEngine:
    def __init__(self, summary: RunSummary):
        self.summary = summary

    async def run(self, alert_type: str) -> RunSummary:
        return self.summary.model_copy(update={"alert_type": alert_type})

    async def run_all(self) -> RunSummary:
        return self.summary


@pytest.fixture
def stub_engine():
    def install(summary: RunSummary):
        app.dependency_overrides[get_alert_engine] = lambda: _StubEngine(summary)

    yield install
    app.dependency_overrides.pop(get_alert_engine, None)


_CRON_ROUTES = [
    "/api/internal/alerts/run-price-checks",
    "/api/internal/alerts/run-technical-checks",
    "/api/internal/alerts/run-reminders",
    "/api/internal/alerts/run-earnings-checks",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _CRON_ROUTES)
async def test_cron_route_returns_502_on_upstream_outage(
    client: AsyncClient, stub_engine, path
):
    stub_engine(RunSummary(alert_type="x", errored=3, upstream_errored=2))

    response = await client.post(path, headers=_auth())

    assert response.status_code == 502
    assert "all 3 evaluations errored" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summary",
    [
        RunSummary(alert_type="x"),  # no active alerts
        RunSummary(alert_type="x", errored=2),  # all errored, none upstream
        RunSummary(alert_type="x", met=1, errored=2, upstream_errored=2),  # partial
        RunSummary(alert_type="x", not_met=4),
    ],
)
async def test_cron_route_returns_200_unless_upstream_outage(
    client: AsyncClient, stub_engine, summary
):
    stub_engine(summary)

    response = await client.post(
        "/api/internal/alerts/run-price-checks", headers=_auth()
    )

    assert response.status_code == 200
    assert response.json()["errored"] == summary.errored


@pytest.mark.asyncio
async def test_manual_evaluate_still_returns_the_summary_on_outage(
    client: AsyncClient, stub_engine
):
    """The debugging surface reports the outcomes rather than failing."""
    stub_engine(RunSummary(alert_type="price_cross", errored=2, upstream_errored=2))

    response = await client.post(
        "/api/alerts/evaluate", json={"alert_type": "price_cross"}
    )

    assert response.status_code == 200
    assert response.json()["errored"] == 2
