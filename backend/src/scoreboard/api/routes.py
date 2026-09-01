"""HTTP routes.

Split by credential type rather than by feature, because that is the boundary
that matters for security: everything under `ingest` is machine-write,
everything under `display` is television-read.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
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


class IngestRep(BaseModel):
    """One person's raw components for the period.

    Rates and averages are not accepted. They are always derived, so a
    customer that sends a rounded close rate cannot skew a team total.
    """

    rep_key: str = Field(min_length=1, max_length=200)
    rep_name: str = Field(min_length=1, max_length=200)
    team: str = ""
    home_branch: str = ""
    title: str = ""
    hire_date: str = ""

    issued_leads: float = 0
    pitched_leads: float = 0
    sold_leads: float = 0
    gross_split: float = 0
    pending_split: float = 0
    net_split: float = 0


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

    records = [
        SourceRecord(
            rep_key=entry.rep_key,
            rep_name=entry.rep_name,
            source_team=entry.team,
            home_branch=entry.home_branch,
            title=entry.title,
            hire_date=entry.hire_date,
            components={key: getattr(entry, key) for key in ADDITIVE},
        )
        for entry in payload.reps
    ]

    result = apply_records(scope, records, period)
    return {
        "ok": True,
        "period": {"start": period.start.isoformat(), "end": period.end.isoformat()},
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
