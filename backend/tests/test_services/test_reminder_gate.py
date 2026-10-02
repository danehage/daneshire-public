"""
DB-free tests for the date-reminder gate in ``AlertEngine._evaluate_one``.

Kept out of ``test_alert_engine.py`` because that module's autouse
cleanup fixture needs a live database; the gate itself is pure.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.alert import Alert
from app.schemas.alert_conditions import Errored, Met, NotMet
from app.services.alert_engine import AlertEngine


class TestReminderGate:
    """DB-free tests of the reminder date gate in ``_evaluate_one``."""

    TODAY = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

    def _evaluate(self, trigger_date: str):
        engine = AlertEngine(None, now=lambda: self.TODAY)  # type: ignore[arg-type]
        alert = Alert(
            ticker="AAPL",
            name="r",
            alert_type="date_reminder",
            condition={"metric": "reminder", "trigger_date": trigger_date},
        )
        return engine._evaluate_one(alert, {"AAPL": None}).outcome

    @pytest.mark.parametrize(
        "trigger_date,expected",
        [
            ("2026-10-02", Met),  # today
            ("2026-10-01", Met),  # missed yesterday — catch up
            ("2026-09-01", Met),  # long-missed
            ("2026-10-03", NotMet),  # future
        ],
    )
    def test_gate(self, trigger_date, expected):
        assert isinstance(self._evaluate(trigger_date), expected)

    def test_unparseable_date_errors(self):
        assert isinstance(self._evaluate("not-a-date"), Errored)
