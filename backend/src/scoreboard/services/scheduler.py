"""Automatic source refresh.

Runs as its own process rather than a thread inside the API, so a slow or
hanging source cannot make the leaderboard stop answering. The API serves
reads; the worker does the waiting.

Each source carries its own interval and its own failure state. One customer's
broken Tableau credential must not stop every other customer's refresh, so
every source is attempted independently and failures are recorded rather than
raised.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from scoreboard.connectors import build
from scoreboard.connectors.base import Period, SourceError
from scoreboard.models import DataSource
from scoreboard.security import decrypt_secret
from scoreboard.services.refresh import apply_records
from scoreboard.tenancy import TenantScope

log = logging.getLogger("scoreboard.scheduler")

# How long a source may run before the worker gives up waiting on this pass.
STUCK_AFTER = timedelta(minutes=30)


# No matter what interval a customer types, we do not hammer their reporting
# system more than once a minute.
MIN_INTERVAL_SECONDS = 60


def is_due(source: DataSource, now: datetime) -> bool:
    """Whether this source should be refreshed on this pass.

    Pure, so the decision can be tested at any point in time without a
    database and without waiting for a clock.
    """
    if not source.is_enabled:
        return False
    try:
        if not build(source.kind).pullable:
            return False
    except SourceError:
        # An unknown kind is skipped rather than crashing the pass; the row
        # keeps whatever status it had, and the other customers still refresh.
        return False
    if source.last_run_at is None:
        return True
    elapsed = (now - source.last_run_at).total_seconds()
    return elapsed >= max(source.refresh_seconds, MIN_INTERVAL_SECONDS)


def due_sources(session: Session, now: datetime | None = None) -> list[DataSource]:
    """Every organization's sources that are ready to run.

    Deliberately a plain query across all tenants. The worker is not acting
    for one customer, it is servicing all of them, which is the single place
    in the system that legitimately crosses the boundary.
    """
    now = now or datetime.now(timezone.utc)
    candidates = session.scalars(
        select(DataSource).where(DataSource.is_enabled.is_(True))
    ).all()
    return [source for source in candidates if is_due(source, now)]


def refresh_source(session: Session, source: DataSource, now: datetime | None = None) -> dict:
    """Refresh one source. Never raises; the outcome is recorded on the row."""
    now = now or datetime.now(timezone.utc)
    scope = TenantScope(session, source.org_id)
    outcome = {"source_id": source.id, "org_id": source.org_id, "ok": False}

    try:
        connector = build(
            source.kind,
            config=source.config,
            secret=decrypt_secret(source.secret_encrypted),
        )
        records = connector.fetch(Period.current_month())
    except (SourceError, ValueError) as exc:
        source.last_run_at = now
        source.last_status = str(exc)[:500]
        session.commit()
        outcome["error"] = str(exc)
        log.warning("source %s (org %s) failed: %s", source.id, source.org_id, exc)
        return outcome
    except Exception as exc:  # noqa: BLE001 - one bad source must not stop the loop
        source.last_run_at = now
        source.last_status = f"Unexpected error: {exc.__class__.__name__}"
        session.commit()
        outcome["error"] = repr(exc)
        log.exception("source %s (org %s) raised", source.id, source.org_id)
        return outcome

    result = apply_records(scope, records, Period.current_month())
    source.last_run_at = now
    source.last_status = f"Loaded {result.reps_seen} reps"
    session.commit()

    outcome.update({"ok": True, **result.as_dict()})
    log.info("source %s (org %s): %s reps", source.id, source.org_id, result.reps_seen)
    return outcome


def run_once(session: Session, now: datetime | None = None) -> list[dict]:
    return [refresh_source(session, source, now) for source in due_sources(session, now)]
