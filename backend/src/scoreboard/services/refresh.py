"""Applying source records to the database.

One path for every source. Whether records arrived from a Tableau pull, a
pushed payload or a CSV upload, they land here and are applied by the same
rules, so no source can develop its own private behaviour.

The rule that matters: a refresh writes numbers and identity fields. It never
writes `Rep.team_id`. Team structure belongs to the customer and survives
every refresh, which is the whole reason the original product existed.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select

from scoreboard.connectors.base import Period, SourceRecord
from scoreboard.domain.metrics import MetricCatalogue
from scoreboard.models import Rep, RepMetrics
from scoreboard.tenancy import TenantScope


class RefreshResult:
    def __init__(self):
        self.reps_seen = 0
        self.reps_created = 0
        self.metrics_written = 0
        self.skipped: list[str] = []

    def as_dict(self) -> dict:
        return {
            "reps_seen": self.reps_seen,
            "reps_created": self.reps_created,
            "metrics_written": self.metrics_written,
            "skipped": self.skipped,
        }


def _components(record: SourceRecord, metrics: MetricCatalogue) -> dict[str, float]:
    """Keep only additive values; a source cannot supply a rate or an average.

    Which values those are is now the organization's own catalogue, so a
    customer's invented metric is stored on exactly the same terms as ours.
    """
    return {key: float(record.components.get(key) or 0) for key in metrics.additive}


def apply_records(
    scope: TenantScope,
    records: list[SourceRecord],
    period: Period,
    captured_on: date | None = None,
    metrics: MetricCatalogue | None = None,
) -> RefreshResult:
    from scoreboard.services.catalogue import catalogue_for

    metrics = metrics or catalogue_for(scope)
    result = RefreshResult()
    captured_on = captured_on or datetime.now(UTC).date()

    existing = {rep.rep_key: rep for rep in scope.all(Rep)}

    for record in records:
        key = (record.rep_key or "").strip()
        if not key or not (record.rep_name or "").strip():
            result.skipped.append(record.rep_key or "(no key)")
            continue

        result.reps_seen += 1
        rep = existing.get(key)
        if rep is None:
            rep = scope.add(
                Rep(
                    rep_key=key,
                    name=record.rep_name,
                    source_team=record.source_team,
                    home_branch=record.home_branch,
                    title=record.title,
                    hire_date=record.hire_date,
                )
            )
            scope.flush()
            existing[key] = rep
            result.reps_created += 1
        else:
            # Identity fields follow the source; team_id deliberately does not.
            rep.name = record.rep_name or rep.name
            rep.source_team = record.source_team or rep.source_team
            rep.home_branch = record.home_branch or rep.home_branch
            rep.title = record.title or rep.title
            rep.hire_date = record.hire_date or rep.hire_date
            rep.is_active = True

        # `stored`, not `metrics`: this is today's row for one person, while
        # `metrics` is the organization's catalogue. Reusing the name made the
        # catalogue become a database row — or None — inside this loop.
        stored = scope.session.scalars(
            select(RepMetrics).where(
                RepMetrics.org_id == scope.org_id,
                RepMetrics.rep_id == rep.id,
                RepMetrics.period_start == period.start,
                RepMetrics.period_end == period.end,
                RepMetrics.captured_on == captured_on,
            )
        ).first()

        values = _components(record, metrics)
        if stored is None:
            scope.add(
                RepMetrics(
                    rep_id=rep.id,
                    period_start=period.start,
                    period_end=period.end,
                    captured_on=captured_on,
                    values=values,
                )
            )
        else:
            stored.values = values
            stored.captured_at = datetime.now(UTC)
        result.metrics_written += 1

    scope.commit()
    return result
