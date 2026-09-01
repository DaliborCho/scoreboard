"""The source boundary.

Every way data can enter the platform implements this interface. Downstream
code — leaderboard, screens, themes — depends on `SourceRecord` and never on a
vendor's shapes, so adding a customer's system means adding one file here
rather than editing anything else.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, date, datetime


@dataclass
class Period:
    start: date
    end: date

    @classmethod
    def current_month(cls, today: date | None = None) -> Period:
        # UTC, not the container's local date. A per-organization timezone is
        # the right answer and is not built yet; an ambiguous "today" that
        # depends on where the server happens to run is the worse of the two.
        today = today or datetime.now(UTC).date()
        first = today.replace(day=1)
        nxt = first.replace(year=first.year + 1, month=1) if first.month == 12 \
            else first.replace(month=first.month + 1)
        return cls(start=first, end=date.fromordinal(nxt.toordinal() - 1))


@dataclass
class SourceRecord:
    """One person's numbers for one period, normalized.

    `components` holds only additive values. Rates and averages are never
    accepted from a source — they are derived, so a source that reports a
    rounded percentage cannot corrupt a team total.
    """

    rep_key: str
    rep_name: str
    source_team: str = ""
    home_branch: str = ""
    title: str = ""
    hire_date: str = ""
    components: dict[str, float] = field(default_factory=dict)


@dataclass
class ConnectionResult:
    ok: bool
    message: str
    detail: dict = field(default_factory=dict)


class SourceError(RuntimeError):
    """Raised for any failure the user could plausibly fix themselves."""


class Connector(ABC):
    kind: str = ""
    label: str = ""
    # False for sources that push to us rather than being pulled from.
    pullable: bool = True

    def __init__(self, config: dict | None = None, secret: str = ""):
        self.config = config or {}
        self.secret = secret

    @abstractmethod
    def test_connection(self) -> ConnectionResult:
        """Verify credentials and reachability without fetching a report."""

    def discover(self) -> dict:
        """Describe what the user can choose from: reports, columns, filters.

        Powers the mapping screen. A connector with nothing to browse may
        return an empty structure.
        """
        return {"reports": [], "columns": [], "filters": []}

    @abstractmethod
    def fetch(self, period: Period) -> list[SourceRecord]:
        """Return normalized records for the period."""
