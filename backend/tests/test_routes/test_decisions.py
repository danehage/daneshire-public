"""Decision log API (#119): create / list / summary / delete, the journal
feed projection, and the database-level insert-only trigger."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models.decision import Decision, DecisionOutcome
from app.models.scan_history import ScanPick
from app.schemas.market import PriceQuote
from app.services.market import TickerNotFound, UpstreamError, get_market

TEST_TICKERS = ("DLGA", "DLGB", "SPY")


class _QuoteMarket:
    def __init__(self, prices: dict[str, float], fail: dict | None = None):
        self.prices = prices
        self.fail = fail or {}
        self.calls: list[list[str]] = []

    async def quotes(self, tickers):
        self.calls.append(list(tickers))
        out = {}
        for t in tickers:
            s = t.upper()
            if s in self.fail:
                out[s] = self.fail[s]
            elif s in self.prices:
                out[s] = PriceQuote(ticker=s, price=self.prices[s])
            else:
                out[s] = TickerNotFound(s)
        return out


async def _wipe(db: AsyncSession):
    await db.execute(delete(DecisionOutcome))
    await db.execute(delete(Decision))
    await db.execute(delete(ScanPick).where(ScanPick.ticker.in_(TEST_TICKERS)))
    await db.commit()


@pytest_asyncio.fixture(autouse=True)
async def cleanup(db_session: AsyncSession):
    await _wipe(db_session)
    yield
    await db_session.rollback()
    await _wipe(db_session)


@pytest.fixture
def market(client):
    fake = _QuoteMarket({"DLGA": 123.456789, "DLGB": 50.0, "SPY": 600.12})
    app.dependency_overrides[get_market] = lambda: fake
    return fake


def _body(**overrides):
    body = {
        "ticker": "dlga",
        "decision": "buy",
        "conviction": 4,
        "thesis": "Beat and raise, pulled back to the 50-day.",
        "invalidation": "Closes below the post-earnings gap.",
    }
    body.update(overrides)
    return body


async def _seed(db: AsyncSession, **kw) -> Decision:
    fields = dict(
        ticker="DLGA",
        decision="buy",
        conviction=3,
        thesis="t",
        invalidation="i",
        price_at_decision=Decimal("100"),
        spy_at_decision=Decimal("500"),
        source="manual",
    )
    fields.update(kw)
    d = Decision(**fields)
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


async def _seed_outcome(db: AsyncSession, decision: Decision, horizon: int, excess, status="scored"):
    db.add(
        DecisionOutcome(
            decision_id=decision.id,
            horizon_sessions=horizon,
            status=status,
            excess_return=None if excess is None else Decimal(str(excess)),
        )
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_captures_both_quotes_server_side(client: AsyncClient, market):
    response = await client.post("/api/decisions", json=_body())
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["ticker"] == "DLGA"
    assert Decimal(data["price_at_decision"]) == Decimal("123.4568")
    assert Decimal(data["spy_at_decision"]) == Decimal("600.1200")
    assert data["source"] == "manual"
    assert data["outcomes"] == []
    assert data["superseded_by_id"] is None
    created = datetime.fromisoformat(data["created_at"])
    assert datetime.fromisoformat(data["deletable_until"]) - created == timedelta(minutes=15)
    # One request for both, so ticker and SPY share one moment.
    assert market.calls == [["DLGA", "SPY"]]


@pytest.mark.parametrize(
    "extra",
    [
        {"price_at_decision": 1.0},
        {"spy_at_decision": 1.0},
        {"created_at": "2020-01-01T00:00:00Z"},
    ],
)
async def test_client_cannot_supply_price_or_timestamp(client: AsyncClient, market, extra):
    response = await client.post("/api/decisions", json=_body(**extra))
    assert response.status_code == 422


@pytest.mark.parametrize(
    "bad",
    [
        {"decision": "sell"},
        {"conviction": 0},
        {"conviction": 6},
        {"thesis": "x" * 281},
        {"invalidation": "   "},
        {"source": "vault"},
        {"ticker": "A&B"},
    ],
)
async def test_validation(client: AsyncClient, market, bad):
    assert (await client.post("/api/decisions", json=_body(**bad))).status_code == 422


@pytest.mark.parametrize("failing", ["DLGA", "SPY"])
async def test_quote_failure_is_502_and_writes_nothing(
    client: AsyncClient, db_session: AsyncSession, failing
):
    fake = _QuoteMarket(
        {"DLGA": 10.0, "SPY": 600.0}, fail={failing: UpstreamError(failing, "boom")}
    )
    app.dependency_overrides[get_market] = lambda: fake
    response = await client.post("/api/decisions", json=_body())
    assert response.status_code == 502
    assert failing in response.json()["detail"]
    assert (await db_session.execute(select(Decision))).scalars().all() == []


async def test_scan_id_resolves_to_the_stored_pick(
    client: AsyncClient, db_session: AsyncSession, market
):
    scan_id = uuid.uuid4()
    pick = ScanPick(
        scan_id=scan_id, universe="combined", ranker="scanner", rank=1,
        ticker="DLGA", price=Decimal("120"), scanned_at=datetime.now(timezone.utc),
    )
    db_session.add(pick)
    await db_session.commit()

    hit = await client.post(
        "/api/decisions", json=_body(source="scanner", scan_id=str(scan_id))
    )
    assert hit.status_code == 201
    assert hit.json()["scan_pick_id"] == str(pick.id)
    assert hit.json()["source"] == "scanner"

    # Not in that scan's record, or not a UUID: recorded without a link.
    miss = await client.post(
        "/api/decisions", json=_body(ticker="DLGB", source="scanner", scan_id=str(scan_id))
    )
    assert miss.status_code == 201 and miss.json()["scan_pick_id"] is None
    junk = await client.post("/api/decisions", json=_body(scan_id="not-a-uuid"))
    assert junk.status_code == 201 and junk.json()["scan_pick_id"] is None


async def test_supersede_links_both_ways(client: AsyncClient, market):
    first = (await client.post("/api/decisions", json=_body())).json()
    second = await client.post(
        "/api/decisions",
        json=_body(decision="pass", conviction=2, supersedes_id=first["id"]),
    )
    assert second.status_code == 201
    assert second.json()["supersedes_id"] == first["id"]

    listed = {d["id"]: d for d in (await client.get("/api/decisions")).json()}
    assert listed[first["id"]]["superseded_by_id"] == second.json()["id"]


async def test_supersede_target_must_exist_on_the_same_ticker(client: AsyncClient, market):
    other = (await client.post("/api/decisions", json=_body(ticker="DLGB"))).json()
    wrong_ticker = await client.post("/api/decisions", json=_body(supersedes_id=other["id"]))
    assert wrong_ticker.status_code == 422
    missing = await client.post(
        "/api/decisions", json=_body(supersedes_id=str(uuid.uuid4()))
    )
    assert missing.status_code == 422


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_list_filters_and_embeds_outcomes(client: AsyncClient, db_session: AsyncSession):
    a = await _seed(db_session, ticker="DLGA", decision="buy")
    await _seed(db_session, ticker="DLGB", decision="pass")
    await _seed_outcome(db_session, a, 63, 0.05)
    await _seed_outcome(db_session, a, 21, -0.01)

    everything = (await client.get("/api/decisions")).json()
    assert {d["ticker"] for d in everything} == {"DLGA", "DLGB"}

    only_a = (await client.get("/api/decisions?ticker=dlga")).json()
    assert [d["ticker"] for d in only_a] == ["DLGA"]
    assert [o["horizon_sessions"] for o in only_a[0]["outcomes"]] == [21, 63]
    assert Decimal(only_a[0]["outcomes"][1]["excess_return"]) == Decimal("0.05")

    passes = (await client.get("/api/decisions?decision=pass")).json()
    assert [d["ticker"] for d in passes] == ["DLGB"]


# ---------------------------------------------------------------------------
# Delete window
# ---------------------------------------------------------------------------


async def test_delete_within_window(client: AsyncClient, db_session: AsyncSession, market):
    created = (await client.post("/api/decisions", json=_body())).json()
    response = await client.delete(f"/api/decisions/{created['id']}")
    assert response.status_code == 204
    assert (await db_session.execute(select(Decision))).scalars().all() == []


async def test_delete_after_window_is_409_forever(client: AsyncClient, db_session: AsyncSession):
    old = await _seed(
        db_session, created_at=datetime.now(timezone.utc) - timedelta(minutes=16)
    )
    response = await client.delete(f"/api/decisions/{old.id}")
    assert response.status_code == 409
    assert "superseding" in response.json()["detail"]


async def test_delete_unknown_is_404(client: AsyncClient):
    assert (await client.delete(f"/api/decisions/{uuid.uuid4()}")).status_code == 404


async def test_there_is_no_patch(client: AsyncClient, db_session: AsyncSession):
    d = await _seed(db_session)
    assert (await client.patch(f"/api/decisions/{d.id}", json={"thesis": "x"})).status_code == 405


async def test_deleting_a_superseded_decision_nulls_the_link(
    client: AsyncClient, db_session: AsyncSession, market
):
    """ON DELETE SET NULL is an UPDATE; the insert-only trigger lets it through."""
    first = (await client.post("/api/decisions", json=_body())).json()
    second = (
        await client.post("/api/decisions", json=_body(supersedes_id=first["id"]))
    ).json()
    assert (await client.delete(f"/api/decisions/{first['id']}")).status_code == 204
    remaining = (await client.get("/api/decisions")).json()
    assert [d["id"] for d in remaining] == [second["id"]]
    assert remaining[0]["supersedes_id"] is None


# ---------------------------------------------------------------------------
# Insert-only, enforced by the database
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE decisions SET thesis = 'rewritten' WHERE id = :id",
        "UPDATE decisions SET price_at_decision = 1 WHERE id = :id",
        "UPDATE decisions SET created_at = now() - interval '1 year' WHERE id = :id",
        # Nulling a link is only allowed as the sole change, not with others.
        "UPDATE decisions SET supersedes_id = NULL, conviction = 5 WHERE id = :id",
        # Pointing a link somewhere new is not a FK action.
        "UPDATE decisions SET supersedes_id = id WHERE id = :id",
    ],
)
async def test_decision_updates_are_rejected(db_session: AsyncSession, sql):
    d = await _seed(db_session)
    with pytest.raises(DBAPIError, match="insert-only"):
        await db_session.execute(text(sql), {"id": d.id})
    await db_session.rollback()


async def test_outcome_updates_are_rejected(db_session: AsyncSession):
    d = await _seed(db_session)
    await _seed_outcome(db_session, d, 21, 0.01)
    with pytest.raises(DBAPIError, match="insert-only"):
        await db_session.execute(
            text("UPDATE decision_outcomes SET excess_return = 0.5 WHERE decision_id = :id"),
            {"id": d.id},
        )
    await db_session.rollback()


async def test_deleting_a_scan_pick_nulls_the_decisions_link(db_session: AsyncSession):
    pick = ScanPick(
        scan_id=uuid.uuid4(), universe="combined", ranker="scanner", rank=1,
        ticker="DLGA", price=Decimal("1"), scanned_at=datetime.now(timezone.utc),
    )
    db_session.add(pick)
    await db_session.commit()
    d = await _seed(db_session, scan_pick_id=pick.id)

    await db_session.execute(delete(ScanPick).where(ScanPick.id == pick.id))
    await db_session.commit()
    refreshed = (
        await db_session.execute(
            select(Decision.scan_pick_id).where(Decision.id == d.id)
        )
    ).scalar_one()
    assert refreshed is None


async def test_duplicate_horizon_is_rejected(db_session: AsyncSession):
    d = await _seed(db_session)
    await _seed_outcome(db_session, d, 21, 0.01)
    with pytest.raises(Exception):
        await _seed_outcome(db_session, d, 21, 0.02)
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


async def test_summary_groups_by_call_conviction_horizon(
    client: AsyncClient, db_session: AsyncSession
):
    a = await _seed(db_session, decision="buy", conviction=4)
    b = await _seed(db_session, decision="buy", conviction=4)
    c = await _seed(db_session, decision="buy", conviction=4, supersedes_id=b.id)
    p = await _seed(db_session, decision="pass", conviction=2)
    await _seed_outcome(db_session, a, 21, 0.10)
    await _seed_outcome(db_session, b, 21, -0.02)                   # superseded: counts
    await _seed_outcome(db_session, c, 21, 0.04, status="truncated")  # truncated: counts
    await _seed_outcome(db_session, a, 63, None, status="no_data")    # excluded
    await _seed_outcome(db_session, p, 21, -0.03)

    data = (await client.get("/api/decisions/summary")).json()
    assert data["horizons"] == [21, 63, 126]
    assert data["min_reliable_n"] == 10
    assert data["total_decisions"] == 4
    assert len(data["cells"]) == 2 * 5 * 3

    cells = {(c["decision"], c["conviction"], c["horizon_sessions"]): c for c in data["cells"]}
    buy4 = cells[("buy", 4, 21)]
    assert buy4["n"] == 3
    assert buy4["mean_excess_return"] == pytest.approx(0.04)
    assert buy4["median_excess_return"] == pytest.approx(0.04)
    assert buy4["hit_rate"] == pytest.approx(2 / 3)

    assert cells[("buy", 4, 63)]["n"] == 0
    assert cells[("buy", 4, 63)]["mean_excess_return"] is None
    assert cells[("pass", 2, 21)]["n"] == 1
    assert cells[("pass", 2, 21)]["hit_rate"] == 0


# ---------------------------------------------------------------------------
# Journal feed
# ---------------------------------------------------------------------------


async def test_decisions_appear_in_the_journal_feed_read_only(
    client: AsyncClient, db_session: AsyncSession
):
    d = await _seed(db_session, ticker="DLGA", decision="pass", conviction=2, thesis="Too extended")

    feed = (await client.get("/api/journal?ticker=DLGA")).json()
    rows = [e for e in feed if e["source"] == "decision"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == f"decision:{d.id}"
    assert row["decision_id"] == str(d.id)
    assert row["entry_type"] == "decision"
    assert "PASS" in row["content"] and "Too extended" in row["content"]

    only = (await client.get("/api/journal?entry_type=decision")).json()
    assert {e["source"] for e in only} == {"decision"}
    theses = (await client.get("/api/journal?entry_type=thesis")).json()
    assert all(e["source"] != "decision" for e in theses)
