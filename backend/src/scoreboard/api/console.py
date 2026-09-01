"""Console routes: what a signed-in person can do.

Grouped by resource, but every mutating endpoint answers the same two
questions before acting — is this person senior enough (rank), and does their
authority cover this particular team (scope). Rank alone would let a team lead
edit a neighbouring team.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from scoreboard.api.deps import auth_context, require_role, user_scope
from scoreboard.connectors import build, catalogue
from scoreboard.connectors.base import Period, SourceError
from scoreboard.db import get_session
from scoreboard.domain.leaderboard import MODES
from scoreboard.models import (
    ApiKey,
    AuditLog,
    Branch,
    DataSource,
    DisplayToken,
    Organization,
    Rep,
    Role,
    Team,
)
from scoreboard.security import encrypt_secret, generate_token
from scoreboard.services import audit
from scoreboard.services.auth import (
    AuthContext,
    authenticate,
    can_edit_team,
    editable_team_ids,
    memberships_for,
    revoke_session,
    start_session,
    switch_organization,
)
from scoreboard.services.board import rows_for_period, trend
from scoreboard.services.refresh import apply_records
from scoreboard.tenancy import TenantScope

router = APIRouter(prefix="/api/v1", tags=["console"])


# ---------------------------------------------------------------- sign in
class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)
    org_id: int | None = None


@router.post("/auth/login")
def login(payload: LoginRequest, session: Session = Depends(get_session)) -> dict:
    user = authenticate(session, payload.email, payload.password)
    if user is None:
        # One message for a wrong password and an unknown address alike, so
        # the endpoint cannot be used to discover who has an account.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password."
        )

    memberships = memberships_for(session, user)
    if not memberships:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account does not belong to any organization.",
        )

    chosen = next((m for m in memberships if m.org_id == payload.org_id), memberships[0])
    token, row = start_session(session, user, chosen.org_id)

    organizations = {
        org.id: org.name
        for org in session.query(Organization)
        .filter(Organization.id.in_([m.org_id for m in memberships]))
        .all()
    }
    return {
        "token": token,
        "expires_at": row.expires_at.isoformat(),
        "user": {"id": user.id, "email": user.email, "full_name": user.full_name},
        "active_org_id": chosen.org_id,
        "role": chosen.role.value,
        "organizations": [
            {"id": m.org_id, "name": organizations.get(m.org_id, ""), "role": m.role.value}
            for m in memberships
        ],
    }


@router.post("/auth/logout")
def logout(
    context: AuthContext = Depends(auth_context), session: Session = Depends(get_session)
) -> dict:
    revoke_session(session, context.session_row)
    return {"ok": True}


@router.get("/auth/me")
def me(
    context: AuthContext = Depends(auth_context), session: Session = Depends(get_session)
) -> dict:
    org = session.get(Organization, context.org_id)
    return {
        "user": {
            "id": context.user.id,
            "email": context.user.email,
            "full_name": context.user.full_name,
        },
        "organization": {"id": org.id, "name": org.name, "slug": org.slug} if org else None,
        "role": context.role.value,
        "scope": {
            "branch_id": context.membership.branch_id,
            "team_id": context.membership.team_id,
        },
        "editable_team_ids": editable_team_ids(session, context),
    }


class SwitchRequest(BaseModel):
    org_id: int


@router.post("/auth/switch-org")
def switch_org(
    payload: SwitchRequest,
    context: AuthContext = Depends(auth_context),
    session: Session = Depends(get_session),
) -> dict:
    switched = switch_organization(session, context, payload.org_id)
    if switched is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not belong to that organization.",
        )
    return {"ok": True, "active_org_id": switched.org_id, "role": switched.role.value}


# ---------------------------------------------------------------- teams
class TeamRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    branch_id: int | None = None
    lead_name: str = ""
    lead_role: str = "Sales Manager"


def _team_json(team: Team) -> dict:
    return {
        "id": team.id,
        "name": team.name,
        "branch_id": team.branch_id,
        "lead_name": team.lead_name,
        "lead_role": team.lead_role,
        "is_active": team.is_active,
    }


@router.get("/teams")
def list_teams(scope: TenantScope = Depends(user_scope)) -> dict:
    return {"teams": [_team_json(t) for t in scope.all(Team)]}


@router.post("/teams", status_code=status.HTTP_201_CREATED)
def create_team(
    payload: TeamRequest,
    context: AuthContext = Depends(require_role(Role.branch_manager)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    if scope.one_by(Team, name=payload.name) is not None:
        raise HTTPException(status_code=409, detail=f"A team named '{payload.name}' exists.")

    branch_id = payload.branch_id
    if context.role == Role.branch_manager:
        # A branch manager creates teams inside their own branch, never elsewhere.
        branch_id = context.membership.branch_id
    if branch_id is not None and scope.get(Branch, branch_id) is None:
        raise HTTPException(status_code=404, detail="Branch not found.")

    team = scope.add(
        Team(
            name=payload.name,
            branch_id=branch_id,
            lead_name=payload.lead_name,
            lead_role=payload.lead_role,
        )
    )
    scope.flush()
    audit.record(
        scope, "team.create", actor_user_id=context.user.id,
        actor_label=context.user.email, target=payload.name,
    )
    scope.commit()
    return _team_json(team)


@router.patch("/teams/{team_id}")
def update_team(
    team_id: int,
    payload: TeamRequest,
    context: AuthContext = Depends(require_role(Role.team_lead)),
    scope: TenantScope = Depends(user_scope),
    session: Session = Depends(get_session),
) -> dict:
    team = scope.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found.")
    if not can_edit_team(session, context, team):
        raise HTTPException(status_code=403, detail="You cannot edit that team.")

    clash = scope.one_by(Team, name=payload.name)
    if clash is not None and clash.id != team.id:
        raise HTTPException(status_code=409, detail=f"A team named '{payload.name}' exists.")

    before = _team_json(team)
    team.name = payload.name
    team.lead_name = payload.lead_name
    team.lead_role = payload.lead_role
    # Moving a team between branches is an org-level decision.
    if payload.branch_id is not None and context.role in (Role.org_admin, Role.owner):
        team.branch_id = payload.branch_id

    audit.record(
        scope, "team.update", actor_user_id=context.user.id,
        actor_label=context.user.email, target=team.name,
        detail={"before": before, "after": _team_json(team)},
    )
    scope.commit()
    return _team_json(team)


class DeleteTeamRequest(BaseModel):
    """Where each member goes. A null destination returns them to Unassigned."""

    reassign: dict[int, int | None] = Field(default_factory=dict)


@router.post("/teams/{team_id}/delete")
def delete_team(
    team_id: int,
    payload: DeleteTeamRequest,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    team = scope.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found.")

    members = [r for r in scope.all(Rep) if r.team_id == team.id]
    unplaced = [r for r in members if r.id not in payload.reassign]
    if unplaced:
        # Deleting a team must never quietly delete or orphan people. The
        # caller is told exactly who still needs a destination.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "Every member needs a destination before this team can be deleted.",
                "unassigned": [{"id": r.id, "name": r.name} for r in unplaced],
            },
        )

    for rep in members:
        destination = payload.reassign.get(rep.id)
        if destination is not None and scope.get(Team, destination) is None:
            raise HTTPException(status_code=404, detail=f"Destination team {destination} not found.")
        rep.team_id = destination

    audit.record(
        scope, "team.delete", actor_user_id=context.user.id,
        actor_label=context.user.email, target=team.name,
        detail={"moved": {str(k): v for k, v in payload.reassign.items()}},
    )
    scope.delete(team)
    scope.commit()
    return {"ok": True, "moved": len(members)}


# ---------------------------------------------------------------- people
@router.get("/reps")
def list_reps(scope: TenantScope = Depends(user_scope)) -> dict:
    teams = {t.id: t.name for t in scope.all(Team)}
    return {
        "reps": [
            {
                "id": rep.id,
                "rep_key": rep.rep_key,
                "name": rep.name,
                "team_id": rep.team_id,
                "team": teams.get(rep.team_id) or rep.source_team or "Unassigned",
                "source_team": rep.source_team,
                "assigned_locally": rep.team_id is not None,
                "home_branch": rep.home_branch,
                "title": rep.title,
            }
            for rep in scope.all(Rep)
        ]
    }


class AssignRequest(BaseModel):
    team_id: int | None = None


@router.post("/reps/{rep_id}/team")
def assign_rep(
    rep_id: int,
    payload: AssignRequest,
    context: AuthContext = Depends(require_role(Role.branch_manager)),
    scope: TenantScope = Depends(user_scope),
    session: Session = Depends(get_session),
) -> dict:
    """Move a person between teams.

    This is the assignment that outranks the source system. Clearing it hands
    the person back to whatever team the source reports.
    """
    rep = scope.get(Rep, rep_id)
    if rep is None:
        raise HTTPException(status_code=404, detail="Rep not found.")

    if payload.team_id is not None:
        team = scope.get(Team, payload.team_id)
        if team is None:
            raise HTTPException(status_code=404, detail="Team not found.")
        if not can_edit_team(session, context, team):
            raise HTTPException(status_code=403, detail="You cannot assign into that team.")

    previous = rep.team_id
    rep.team_id = payload.team_id
    audit.record(
        scope, "rep.assign", actor_user_id=context.user.id,
        actor_label=context.user.email, target=rep.name,
        detail={"from_team_id": previous, "to_team_id": payload.team_id},
    )
    scope.commit()
    return {"ok": True, "rep_id": rep.id, "team_id": rep.team_id}


# ---------------------------------------------------------------- board preview
@router.get("/board")
def board_preview(
    mode: str = Query(default="whole_office"),
    rank_by: str = Query(default="net_split"),
    team: str = Query(default=""),
    teams: list[str] = Query(default=[]),
    period_start: date | None = None,
    period_end: date | None = None,
    scope: TenantScope = Depends(user_scope),
) -> dict:
    """The same payload a television gets, for previewing before publishing."""
    builder = MODES.get(mode)
    if builder is None:
        raise HTTPException(status_code=404, detail=f"Unknown mode '{mode}'.")

    period = (
        Period(start=period_start, end=period_end)
        if period_start and period_end
        else Period.current_month()
    )
    rows = rows_for_period(scope, period)

    if mode == "per_team":
        payload = builder(rows, team, rank_by)
    elif mode == "team_vs_team":
        payload = builder(rows, teams, rank_by)
    else:
        payload = builder(rows, rank_by)

    payload["period"] = {"start": period.start.isoformat(), "end": period.end.isoformat()}
    payload["rep_count"] = len(rows)
    return payload


@router.get("/board/trend")
def board_trend(
    metric: str = Query(default="net_split"),
    period_start: date | None = None,
    period_end: date | None = None,
    scope: TenantScope = Depends(user_scope),
) -> dict:
    period = (
        Period(start=period_start, end=period_end)
        if period_start and period_end
        else Period.current_month()
    )
    return {"metric": metric, "points": trend(scope, period, metric)}


# ---------------------------------------------------------------- credentials
class NamedRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@router.get("/api-keys")
def list_api_keys(
    _: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    return {
        "keys": [
            {
                "id": k.id, "name": k.name, "prefix": k.prefix,
                "created_at": k.created_at.isoformat(),
                "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
                "revoked": k.revoked_at is not None,
            }
            for k in scope.all(ApiKey)
        ]
    }


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
def create_api_key(
    payload: NamedRequest,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    token, prefix, hashed = generate_token("sb")
    key = scope.add(ApiKey(name=payload.name, prefix=prefix, token_hash=hashed))
    scope.flush()
    audit.record(
        scope, "api_key.create", actor_user_id=context.user.id,
        actor_label=context.user.email, target=payload.name,
    )
    scope.commit()
    # The only time the full value exists outside the client's hands.
    return {"id": key.id, "name": key.name, "token": token}


@router.post("/api-keys/{key_id}/revoke")
def revoke_api_key(
    key_id: int,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    from datetime import datetime, timezone

    key = scope.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found.")
    key.revoked_at = datetime.now(timezone.utc)
    audit.record(
        scope, "api_key.revoke", actor_user_id=context.user.id,
        actor_label=context.user.email, target=key.name,
    )
    scope.commit()
    return {"ok": True}


@router.get("/display-tokens")
def list_display_tokens(
    _: AuthContext = Depends(require_role(Role.branch_manager)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    return {
        "displays": [
            {
                "id": d.id, "name": d.name, "prefix": d.prefix, "screen_id": d.screen_id,
                "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
                "revoked": d.revoked_at is not None,
            }
            for d in scope.all(DisplayToken)
        ]
    }


@router.post("/display-tokens", status_code=status.HTTP_201_CREATED)
def create_display_token(
    payload: NamedRequest,
    context: AuthContext = Depends(require_role(Role.branch_manager)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    token, prefix, hashed = generate_token("tv")
    display = scope.add(DisplayToken(name=payload.name, prefix=prefix, token_hash=hashed))
    scope.flush()
    audit.record(
        scope, "display_token.create", actor_user_id=context.user.id,
        actor_label=context.user.email, target=payload.name,
    )
    scope.commit()
    return {
        "id": display.id,
        "name": display.name,
        "url": f"/api/v1/display/{token}/board",
        "token": token,
    }


@router.post("/display-tokens/{display_id}/revoke")
def revoke_display_token(
    display_id: int,
    context: AuthContext = Depends(require_role(Role.branch_manager)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    from datetime import datetime, timezone

    display = scope.get(DisplayToken, display_id)
    if display is None:
        raise HTTPException(status_code=404, detail="Display not found.")
    display.revoked_at = datetime.now(timezone.utc)
    audit.record(
        scope, "display_token.revoke", actor_user_id=context.user.id,
        actor_label=context.user.email, target=display.name,
    )
    scope.commit()
    return {"ok": True}


# ---------------------------------------------------------------- sources
class SourceRequest(BaseModel):
    kind: str
    name: str = Field(min_length=1, max_length=200)
    config: dict = Field(default_factory=dict)
    secret: str = ""
    refresh_seconds: int = 900


def _source_json(source: DataSource) -> dict:
    return {
        "id": source.id,
        "kind": source.kind,
        "name": source.name,
        "config": source.config,
        "has_secret": bool(source.secret_encrypted),
        "is_enabled": source.is_enabled,
        "refresh_seconds": source.refresh_seconds,
        "last_run_at": source.last_run_at.isoformat() if source.last_run_at else None,
        "last_status": source.last_status,
    }


@router.get("/sources")
def list_sources(
    _: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    # `has_secret` rather than the secret. A stored credential never comes back out.
    return {"sources": [_source_json(s) for s in scope.all(DataSource)],
            "available": catalogue()}


@router.post("/sources", status_code=status.HTTP_201_CREATED)
def create_source(
    payload: SourceRequest,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    try:
        build(payload.kind)
    except SourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    source = scope.add(
        DataSource(
            kind=payload.kind,
            name=payload.name,
            config=payload.config,
            secret_encrypted=encrypt_secret(payload.secret) if payload.secret else "",
            refresh_seconds=payload.refresh_seconds,
        )
    )
    scope.flush()
    audit.record(
        scope, "source.create", actor_user_id=context.user.id,
        actor_label=context.user.email, target=payload.name,
        detail={"kind": payload.kind},
    )
    scope.commit()
    return _source_json(source)


def _connector_for(scope: TenantScope, source: DataSource):
    from scoreboard.security import decrypt_secret

    return build(source.kind, config=source.config, secret=decrypt_secret(source.secret_encrypted))


@router.post("/sources/{source_id}/test")
def test_source(
    source_id: int,
    _: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    source = scope.get(DataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found.")
    try:
        result = _connector_for(scope, source).test_connection()
    except SourceError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": result.ok, "message": result.message, "detail": result.detail}


@router.post("/sources/{source_id}/refresh")
def refresh_source(
    source_id: int,
    period_start: date | None = None,
    period_end: date | None = None,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    from datetime import datetime, timezone

    source = scope.get(DataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found.")

    connector = _connector_for(scope, source)
    if not connector.pullable:
        raise HTTPException(
            status_code=422,
            detail=f"'{source.kind}' pushes data to us; there is nothing to pull.",
        )

    period = (
        Period(start=period_start, end=period_end)
        if period_start and period_end
        else Period.current_month()
    )

    try:
        records = connector.fetch(period)
    except SourceError as exc:
        source.last_run_at = datetime.now(timezone.utc)
        source.last_status = str(exc)[:500]
        scope.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    result = apply_records(scope, records, period)
    source.last_run_at = datetime.now(timezone.utc)
    source.last_status = f"Loaded {result.reps_seen} reps"
    audit.record(
        scope, "source.refresh", actor_user_id=context.user.id,
        actor_label=context.user.email, target=source.name, detail=result.as_dict(),
    )
    scope.commit()
    return {"ok": True, **result.as_dict()}


# ---------------------------------------------------------------- audit
@router.get("/audit")
def list_audit(
    limit: int = Query(default=100, le=500),
    _: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    entries = scope.session.scalars(
        scope.select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    ).all()
    return {
        "entries": [
            {
                "id": e.id,
                "action": e.action,
                "actor": e.actor_label,
                "target": e.target,
                "detail": e.detail,
                "at": e.created_at.isoformat(),
            }
            for e in entries
        ]
    }

class SourceUpdate(BaseModel):
    name: str | None = None
    config: dict | None = None
    # Blank means "leave the stored credential alone". There is no way to read
    # one back, so an edit form cannot round-trip it and must not clear it.
    secret: str | None = None
    is_enabled: bool | None = None
    refresh_seconds: int | None = None


@router.patch("/sources/{source_id}")
def update_source(
    source_id: int,
    payload: SourceUpdate,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    source = scope.get(DataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found.")

    if payload.name is not None:
        source.name = payload.name
    if payload.config is not None:
        source.config = payload.config
    if payload.is_enabled is not None:
        source.is_enabled = payload.is_enabled
    if payload.refresh_seconds is not None:
        source.refresh_seconds = max(payload.refresh_seconds, 60)
    if payload.secret:
        source.secret_encrypted = encrypt_secret(payload.secret)

    audit.record(
        scope, "source.update", actor_user_id=context.user.id,
        actor_label=context.user.email, target=source.name,
        detail={"secret_replaced": bool(payload.secret)},
    )
    scope.commit()
    return _source_json(source)


@router.post("/sources/{source_id}/delete")
def delete_source(
    source_id: int,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    """Remove a source and its stored credential.

    The numbers it already loaded stay. Deleting a connection should not wipe
    a month of a customer's leaderboard.
    """
    source = scope.get(DataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found.")

    audit.record(
        scope, "source.delete", actor_user_id=context.user.id,
        actor_label=context.user.email, target=source.name,
        detail={"kind": source.kind},
    )
    scope.delete(source)
    scope.commit()
    return {"ok": True}
