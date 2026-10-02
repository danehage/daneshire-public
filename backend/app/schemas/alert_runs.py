"""
Result schemas for ``AlertEngine.run`` / ``run_all``.

Returned by the four internal cron endpoints and the manual
``POST /api/alerts/evaluate``. Per-alert outcomes are included so the
manual endpoint can be used as a debugging surface.
"""

from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


OutcomeStatus = Literal["met", "not_met", "errored"]


class AlertOutcomeRecord(BaseModel):
    """One alert × one evaluation, flattened for transport.

    Mirrors :class:`app.schemas.alert_conditions.Outcome` but as a single
    Pydantic shape so the API consumer doesn't have to discriminate.
    """

    model_config = ConfigDict(frozen=True)

    alert_id: UUID
    ticker: str
    status: OutcomeStatus
    actual_value: Optional[float] = None
    reason: Optional[str] = None


class RunSummary(BaseModel):
    """Aggregate result of ``AlertEngine.run``.

    ``alert_type`` echoes the input filter; ``run_all`` sets it to
    ``"all"``. Counts always sum to ``len(outcomes)``.
    """

    model_config = ConfigDict(frozen=True)

    alert_type: str
    met: int = 0
    not_met: int = 0
    errored: int = 0
    notifications_sent: int = 0
    outcomes: list[AlertOutcomeRecord] = []

    # How many of ``errored`` were provider failures (FMP outage, bad key,
    # rate limit) rather than the alert's own fault (unknown ticker, bad
    # condition). Internal only: excluded from the response body.
    upstream_errored: int = Field(default=0, exclude=True)

    @property
    def all_errored(self) -> bool:
        """At least one alert was evaluated and every evaluation errored."""
        return self.errored > 0 and self.met == 0 and self.not_met == 0

    @property
    def upstream_outage(self) -> bool:
        """Every evaluation errored and the provider is to blame.

        The signature of e.g. a revoked FMP key. The cron routes turn it
        into a 502 so Cloud Scheduler records a failed run. A run whose
        only errors are the alerts' own (a delisted ticker) is not an
        outage and must not fail the job every 15 minutes.
        """
        return self.all_errored and self.upstream_errored > 0

    def merged_with(self, other: "RunSummary") -> "RunSummary":
        return RunSummary(
            alert_type=self.alert_type if self.alert_type == other.alert_type else "all",
            met=self.met + other.met,
            not_met=self.not_met + other.not_met,
            errored=self.errored + other.errored,
            upstream_errored=self.upstream_errored + other.upstream_errored,
            notifications_sent=self.notifications_sent + other.notifications_sent,
            outcomes=[*self.outcomes, *other.outcomes],
        )
