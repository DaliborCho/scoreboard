"""Reading stored data back out as leaderboard rows.

Resolves the ownership split at query time: a rep's displayed team is the
locally assigned one when there is one, and the team the source reported
otherwise. That fallback is what lets a customer start seeing a useful board
before anyone has opened the team builder.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from scoreboard.connectors.base import Period
from scoreboard.domain.leaderboard import UNASSIGNED, RepRow
from scoreboard.models import Rep, RepMetrics, Team
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
    scope: TenantScope, period: Period, captured_on: date | None = None
) -> list[dict]:
    captured_on = captured_on or _latest_capture(scope, period)
    if captured_on is None:
        return []

    team_names = {team.id: team.name for team in scope.all(Team)}

    statement = (
        select(Rep, RepMetrics)
        .join(RepMetrics, RepMetrics.rep_id == Rep.id)
        .where(
            Rep.org_id == scope.org_id,
            RepMetrics.org_id == scope.org_id,
            RepMetrics.period_start == period.start,
            RepMetrics.period_end == period.end,
            RepMetrics.captured_on == captured_on,
            Rep.is_active.is_(True),
        )
    )

    rows = []
    for rep, metrics in scope.session.execute(statement).all():
        team = team_names.get(rep.team_id) or rep.source_team or UNASSIGNED
        rows.append(
            RepRow(
                rep_key=rep.rep_key,
                rep_name=rep.name,
                team=team,
                home_branch=rep.home_branch,
                title=rep.title,
                hire_date=rep.hire_date,
                components=dict(metrics.values or {}),
            ).as_row()
        )
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

    from scoreboard.domain.metrics import roll_up

    return [
        {"date": day.isoformat(), "value": roll_up(entries).get(metric, 0.0)}
        for day, entries in sorted(by_day.items())
    ]
