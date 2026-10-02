"""
Market-hours gate for the scheduled price / technical alert checks.

DB-free: ``is_regular_session`` is pure, and the route helper is driven with
a fake engine.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
import pytz

from app.routes.internal import _run_during_session
from app.schemas.alert_runs import RunSummary
from app.services.indicators import is_regular_session

ET = pytz.timezone("America/New_York")


def et(y, mo, d, h, mi, s=0):
    return ET.localize(datetime(y, mo, d, h, mi, s))


# 2026-09-30 is a Wednesday; 2026-10-03 a Saturday; 2026-10-04 a Sunday.
@pytest.mark.parametrize(
    "when, expected",
    [
        (et(2026, 9, 30, 9, 0), False),  # first cron tick of the 9-16 window
        (et(2026, 9, 30, 9, 29, 59), False),
        (et(2026, 9, 30, 9, 30), True),
        (et(2026, 9, 30, 12, 0), True),
        (et(2026, 9, 30, 16, 0), True),
        (et(2026, 9, 30, 16, 0, 30), True),  # scheduler tick lands late
        (et(2026, 9, 30, 16, 1), False),
        (et(2026, 9, 30, 16, 45), False),  # last cron tick of the 9-16 window
        (et(2026, 10, 3, 11, 0), False),  # Saturday
        (et(2026, 10, 4, 11, 0), False),  # Sunday
        (datetime(2026, 9, 30, 9, 29), False),  # naive is taken as ET
        (datetime(2026, 9, 30, 9, 30), True),
    ],
)
def test_is_regular_session_boundaries(when, expected):
    assert is_regular_session(when) is expected


def test_aware_non_et_datetime_is_converted():
    # 13:30 UTC == 09:30 EDT; 13:29 UTC == 09:29 EDT.
    assert is_regular_session(datetime(2026, 9, 30, 13, 30, tzinfo=timezone.utc))
    assert not is_regular_session(datetime(2026, 9, 30, 13, 29, tzinfo=timezone.utc))
    # Winter (EST): 14:30 UTC == 09:30 EST.
    assert is_regular_session(datetime(2026, 12, 2, 14, 30, tzinfo=timezone.utc))
    assert not is_regular_session(datetime(2026, 12, 2, 14, 29, tzinfo=timezone.utc))


class FakeEngine:
    def __init__(self):
        self.runs: list[str] = []

    async def run(self, alert_type):
        self.runs.append(alert_type)
        return RunSummary(alert_type=alert_type, met=1)


@pytest.mark.parametrize("alert_type", ["price_cross", "technical_signal"])
async def test_gate_skips_outside_session(alert_type, caplog):
    engine = FakeEngine()
    with caplog.at_level(logging.INFO, logger="app.routes.internal"):
        summary = await _run_during_session(engine, alert_type, et(2026, 9, 30, 16, 15))

    assert engine.runs == []
    assert summary == RunSummary(alert_type=alert_type)
    assert "skipped: outside market hours" in caplog.text


async def test_gate_runs_inside_session():
    engine = FakeEngine()
    summary = await _run_during_session(engine, "price_cross", et(2026, 9, 30, 9, 30))

    assert engine.runs == ["price_cross"]
    assert summary.met == 1
