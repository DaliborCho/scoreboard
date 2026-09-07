"""Reading stored data back out as leaderboard rows.

Resolves the ownership split at query time: a person's group is the one the
customer assigned them to, and the one the source reported otherwise. That
fallback is what lets a customer see a useful board before anyone has opened
the group builder.

Which axis a board is grouped by — team, branch, region — is a parameter, not
a table. Every row also carries all of its groups, so a chart can break the
same figures down a different way without a second query.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from scoreboard.connectors.base import Period
from scoreboard.domain.leaderboard import UNASSIGNED, RepRow
from scoreboard.models import Rep, RepMetrics
from scoreboard.services import groups as grp
from scoreboard.tenancy import TenantScope


def _latest_capture(scope: TenantScope, period: Period) -> date | None:
    """The most recent day we captured numbers for this period."""
    return scope.session.scalar(
        select(func.max(RepMetrics.captured_on)).where(
            RepMetrics.org_id == scope.org_id,
            RepMetrics.period_start == period.start,
            RepMetrics.period_end == period.end,
        )
    )


def rows_for_period(
    scope: TenantScope,
    period: Period,
    captured_on: date | None = None,
    metrics=None,
    group_by: str = "",
) -> list[dict]:
    from scoreboard.services.catalogue import catalogue_for

    metrics = metrics or catalogue_for(scope)
    axis = group_by or grp.primary_key(scope)
    assignments = grp.memberships_for(scope)
    # Names a customer deliberately removed. Without this the source puts them
    # straight back, because the fallback below trusts whatever it reports.
    retired = grp.retired_names(scope)

    statement = (
        select(Rep, RepMetrics)
        .join(RepMetrics, RepMetrics.rep_id == Rep.id)
        .where(
            Rep.org_id == scope.org_id,
            RepMetrics.org_id == scope.org_id,
            RepMetrics.period_start == period.start,
            RepMetrics.period_end == period.end,
            Rep.is_active.is_(True),
        )
    )

    if captured_on is not None:
        statement = statement.where(RepMetrics.captured_on == captured_on)
    else:
        # Each person's own most recent capture, not one global latest day.
        #
        # Reading a single day meant a partial update erased everybody who was
        # not in it: pushing one person's figures took the other twelve off the
        # board until the next full refresh. On a wall that reads as the system
        # having lost the team.
        statement = statement.distinct(RepMetrics.rep_id).order_by(
            RepMetrics.rep_id, RepMetrics.captured_on.desc()
        )

    rows = []
    # `captured`, not `metrics`: the loop variable is one rep's stored figures,
    # and `metrics` is now the organization's catalogue. Naming both the same
    # made the catalogue silently become a database row inside this loop.
    for rep, captured in scope.session.execute(statement).all():
        mine = {key: group.name for key, group in (assignments.get(rep.id) or {}).items()}

        # What the source said is a fallback for the shipped axes only. A
        # customer-invented grouping has no source column to fall back to, and
        # inventing one would put people in groups nobody assigned them to.
        #
        # A retired name is never accepted from the source. Deleting a group
        # has to mean it is gone, not gone until the next refresh.
        for key, reported in ((grp.TEAM, rep.source_team), (grp.BRANCH, rep.home_branch)):
            if key not in mine and reported and reported not in retired.get(key, ()):
                mine[key] = reported

        rows.append(
            RepRow(
                rep_key=rep.rep_key,
                rep_name=rep.name,
                group=mine.get(axis) or UNASSIGNED,
                groups=mine,
                home_branch=rep.home_branch,
                title=rep.title,
                hire_date=rep.hire_date,
                components=dict(captured.values or {}),
            ).as_row(metrics)
        )
        # How old this person's figures are, so a board can say so rather than
        # presenting last week's number as today's.
        rows[-1]["captured_on"] = captured.captured_on.isoformat()
    return rows


def trend(scope: TenantScope, period: Period, metric: str) -> list[dict]:
    """Daily totals across the period, for trend charts.

    Available because every refresh writes a dated row rather than
    overwriting one. History cannot be reconstructed after the fact, which is
    why the capture-per-day shape was chosen before there was a chart to
    render it.
    """
    statement = (
        select(RepMetrics.captured_on, RepMetrics.values)
        .where(
            RepMetrics.org_id == scope.org_id,
            RepMetrics.period_start == period.start,
            RepMetrics.period_end == period.end,
        )
        .order_by(RepMetrics.captured_on)
    )

    by_day: dict[date, list[dict]] = {}
    for captured_on, values in scope.session.execute(statement).all():
        by_day.setdefault(captured_on, []).append(dict(values or {}))

    from scoreboard.services.catalogue import catalogue_for

    metrics = catalogue_for(scope)
    return [
        {"date": day.isoformat(), "value": metrics.roll_up(entries).get(metric, 0.0)}
        for day, entries in sorted(by_day.items())
    ]
