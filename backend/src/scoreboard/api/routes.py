"""HTTP routes.

Split by credential type rather than by feature, because that is the boundary
that matters for security: everything under `ingest` is machine-write,
everything under `display` is television-read.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from scoreboard.api.deps import api_key_scope, display_scope
from scoreboard.connectors import catalogue
from scoreboard.connectors.base import Period, SourceRecord
from scoreboard.db import get_session
from scoreboard.domain.leaderboard import MODES
from scoreboard.domain.metrics import ADDITIVE, METRIC_DEFS
from scoreboard.services.board import rows_for_period, trend
from scoreboard.services.refresh import apply_records
from scoreboard.tenancy import TenantScope

# ---------------------------------------------------------------- system
system = APIRouter(tags=["system"])


@system.get("/health")
def health(session=Depends(get_session)) -> dict:
    try:
        session.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:  # pragma: no cover - only hit when the db is down
        database = f"error: {exc.__class__.__name__}"
    return {"status": "ok", "database": database}


@system.get("/api/v1/metrics")
def metric_catalogue() -> dict:
    """What the console offers as columns, ranking options and chart series."""
    return {
        "additive": list(ADDITIVE),
        "metrics": [
            {"key": m.key, "label": m.label, "short": m.short_label, "kind": m.kind}
            for m in METRIC_DEFS
        ],
    }


@system.get("/api/v1/connectors")
def connector_catalogue() -> dict:
    return {"connectors": catalogue()}


# ---------------------------------------------------------------- ingest
ingest = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])


IDENTITY = ("rep_key", "rep_name", "team", "home_branch", "title", "hire_date")


class IngestRep(BaseModel):
    """One person's raw components for the period.

    Any additional key is read as a stored value, and kept only if the
    organization's catalogue has a field by that name. That is what lets a
    customer send a metric they invented without us shipping a schema for it.

    Rates and averages are still not accepted. They are always derived, so a
    customer that sends a rounded close rate cannot skew a team total.
    """

    model_config = ConfigDict(extra="allow")

    rep_key: str = Field(min_length=1, max_length=200)
    rep_name: str = Field(min_length=1, max_length=200)
    team: str = ""
    home_branch: str = ""
    title: str = ""
    hire_date: str = ""

    def components(self, additive: tuple[str, ...]) -> dict[str, float]:
        supplied = self.model_dump()
        values = {}
        for key in additive:
            raw = supplied.get(key)
            try:
                values[key] = float(raw) if raw is not None else 0.0
            except (TypeError, ValueError):
                # A number that will not parse is left at zero rather than
                # rejecting the whole payload, which would lose everybody
                # else's figures over one bad cell.
                values[key] = 0.0
        return values

    def unknown_keys(self, additive: tuple[str, ...]) -> list[str]:
        return sorted(
            key for key in (self.model_extra or {})
            if key not in additive and key not in IDENTITY
        )


class IngestPayload(BaseModel):
    period_start: date | None = None
    period_end: date | None = None
    reps: list[IngestRep] = Field(min_length=1, max_length=5000)


@ingest.post("/reps")
def ingest_reps(payload: IngestPayload, scope: TenantScope = Depends(api_key_scope)) -> dict:
    period = (
        Period(start=payload.period_start, end=payload.period_end)
        if payload.period_start and payload.period_end
        else Period.current_month()
    )
    if period.start > period.end:
        raise HTTPException(status_code=422, detail="period_start is after period_end.")

    from scoreboard.services.catalogue import catalogue_for

    metrics = catalogue_for(scope)
    records = [
        SourceRecord(
            rep_key=entry.rep_key,
            rep_name=entry.rep_name,
            source_team=entry.team,
            home_branch=entry.home_branch,
            title=entry.title,
            hire_date=entry.hire_date,
            components=entry.components(metrics.additive),
        )
        for entry in payload.reps
    ]

    result = apply_records(scope, records, period, metrics=metrics)

    # Named rather than silently dropped: a customer who sends `revenue` when
    # their catalogue calls it `net_split` would otherwise see zeros with no
    # explanation.
    ignored = sorted({key for entry in payload.reps
                      for key in entry.unknown_keys(metrics.additive)})
    return {
        "ok": True,
        "period": {"start": period.start.isoformat(), "end": period.end.isoformat()},
        "accepted_fields": list(metrics.additive),
        "ignored_fields": ignored,
        **result.as_dict(),
    }


# ---------------------------------------------------------------- display
display = APIRouter(prefix="/api/v1/display", tags=["display"])


def _period(period_start: date | None, period_end: date | None) -> Period:
    if period_start and period_end:
        return Period(start=period_start, end=period_end)
    return Period.current_month()


@display.get("/{token}/board")
def board(
    mode: str = Query(default="whole_office"),
    rank_by: str = Query(default="net_split"),
    team: str = Query(default=""),
    teams: list[str] = Query(default=[]),
    period_start: date | None = None,
    period_end: date | None = None,
    scoped=Depends(display_scope),
) -> dict:
    scope, display_token = scoped
    builder = MODES.get(mode)
    if builder is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown mode '{mode}'. Known: {', '.join(MODES)}"
        )

    period = _period(period_start, period_end)
    rows = rows_for_period(scope, period)

    if mode == "per_team":
        payload = builder(rows, team, rank_by)
    elif mode == "team_vs_team":
        payload = builder(rows, teams, rank_by)
    else:
        payload = builder(rows, rank_by)

    payload["display"] = {"name": display_token.name}
    payload["period"] = {"start": period.start.isoformat(), "end": period.end.isoformat()}
    payload["rep_count"] = len(rows)
    return payload


@display.get("/{token}/trend")
def board_trend(
    metric: str = Query(default="net_split"),
    period_start: date | None = None,
    period_end: date | None = None,
    scoped=Depends(display_scope),
) -> dict:
    scope, _ = scoped
    period = _period(period_start, period_end)
    return {"metric": metric, "points": trend(scope, period, metric)}


@display.get("/{token}/screen")
def display_screen(
    screen_id: int | None = Query(default=None),
    period_start: date | None = None,
    period_end: date | None = None,
    scoped=Depends(display_scope),
) -> dict:
    """Everything a television needs, in one request.

    A screen that has not been attached to a display falls back to the whole
    office board, so a newly created display link shows something real rather
    than an error nobody is present to read.
    """
    from scoreboard.models import Screen
    from scoreboard.services.screens import render

    scope, display_token = scoped
    period = _period(period_start, period_end)

    rotation = display_token.rotation or {}
    # Only ids still in the configured cycle are honoured, so a stale link a
    # television kept from an earlier configuration cannot pin it to a screen
    # that was removed from the rotation.
    cycle = [
        sid for sid in (rotation.get("screen_ids") or [])
        if scope.get(Screen, sid) is not None
    ]

    wanted = screen_id if screen_id in cycle else (cycle[0] if cycle else display_token.screen_id)
    screen = scope.get(Screen, wanted) if wanted else None

    payload = render(scope, screen, period=period)
    payload["display"] = {"name": display_token.name}
    payload["rotation"] = (
        {
            "screen_ids": cycle,
            "seconds": int(rotation.get("seconds") or 30),
            "current": wanted,
            "next": cycle[(cycle.index(wanted) + 1) % len(cycle)] if wanted in cycle else None,
        }
        if len(cycle) > 1
        else None
    )
    return payload
