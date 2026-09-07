"""Console routes: what a signed-in person can do.

Grouped by resource, but every mutating endpoint answers the same two
questions before acting — is this person senior enough (rank), and does their
authority cover this particular group (scope). Rank alone would let a team lead
edit a neighbouring team.
"""
from __future__ import annotations

from datetime import UTC, date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from scoreboard.api.deps import auth_context, require_role, user_scope
from scoreboard.connectors import build, catalogue
from scoreboard.connectors.base import Period, SourceError
from scoreboard.db import get_session
from scoreboard.domain.leaderboard import MODES, canonical_mode
from scoreboard.models import (
    ApiKey,
    AuditLog,
    DataSource,
    DisplayToken,
    Group,
    GroupMembership,
    GroupType,
    Organization,
    Rep,
    Role,
)
from scoreboard.security import encrypt_secret, generate_token
from scoreboard.services import audit, ratelimit
from scoreboard.services import groups as grp
from scoreboard.services.auth import (
    AuthContext,
    authenticate,
    can_edit_group,
    editable_group_ids,
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
    # An identifier, not necessarily an address: the platform operator and the
    # demo accounts sign in with a handle. Email format is validated where an
    # account is created with a mailbox, not here.
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1)
    org_id: int | None = None


@router.post("/auth/login")
def login(
    payload: LoginRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    # The account bucket is the tight one: an attacker has to fill it to make
    # progress. The address bucket is loose, because a whole company behind
    # one NAT — or every user behind one proxy — shares it.
    caller = request.client.host if request.client else "unknown"
    email_bucket = f"login:email:{payload.email.lower()}"
    ip_bucket = f"login:ip:{caller}"

    for bucket, limit, window in (
        (email_bucket, ratelimit.LOGIN_LIMIT, ratelimit.LOGIN_WINDOW),
        (ip_bucket, ratelimit.LOGIN_IP_LIMIT, ratelimit.LOGIN_IP_WINDOW),
    ):
        try:
            ratelimit.check(bucket, limit, window)
        except ratelimit.RateLimited as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(exc),
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc

    user = authenticate(session, payload.email, payload.password)
    if user is None:
        # Only failures are counted. Charging a successful sign-in against the
        # budget slowly locks out a busy office during a normal morning.
        ratelimit.record(email_bucket, ratelimit.LOGIN_WINDOW)
        ratelimit.record(ip_bucket, ratelimit.LOGIN_IP_WINDOW)
        # One message for a wrong password and an unknown address alike, so
        # the endpoint cannot be used to discover who has an account.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password."
        )

    memberships = memberships_for(session, user)
    if memberships:
        # A suspended customer cannot sign in at all. Ending their sessions
        # without this would mean the suspension lasted until someone pressed
        # the login button again.
        active = {
            org.id
            for org in session.scalars(
                select(Organization).where(
                    Organization.id.in_([m.org_id for m in memberships]),
                    Organization.is_active.is_(True),
                )
            ).all()
        }
        blocked = len(memberships) - len(active)
        memberships = [m for m in memberships if m.org_id in active]
        if blocked and not memberships and not user.is_superadmin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This account is suspended. Contact whoever runs this installation.",
            )

    if not memberships and not user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account does not belong to any organization.",
        )

    ratelimit.clear(email_bucket)

    # A platform operator with no memberships gets a session that belongs to
    # no organization. Everything tenant-scoped refuses it by design.
    chosen = next((m for m in memberships if m.org_id == payload.org_id), None)
    if chosen is None and memberships:
        chosen = memberships[0]
    token, row = start_session(session, user, chosen.org_id if chosen else None)

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
        "is_operator": user.is_superadmin,
        "active_org_id": chosen.org_id if chosen else None,
        "role": chosen.role.value if chosen else None,
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
    org = session.get(Organization, context.org_id) if context.org_id else None
    return {
        "user": {
            "id": context.user.id,
            "email": context.user.email,
            "full_name": context.user.full_name,
        },
        "is_operator": context.user.is_superadmin,
        "organization": {"id": org.id, "name": org.name, "slug": org.slug} if org else None,
        "role": context.role.value if context.role else None,
        "scope": {"group_id": context.membership.group_id if context.membership else None},
        "editable_group_ids": editable_group_ids(session, context),
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


# ---------------------------------------------------------------- groupings
class GroupTypeRequest(BaseModel):
    key: str = Field(default="", max_length=40)
    label: str = Field(min_length=1, max_length=80)
    plural_label: str = Field(default="", max_length=80)


class GroupRequest(BaseModel):
    type_id: int | None = None
    name: str = Field(min_length=1, max_length=200)
    parent_id: int | None = None
    lead_name: str = ""
    lead_role: str = "Sales Manager"


def _refuse_group(error: grp.GroupError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error))


def _type_json(kind: GroupType) -> dict:
    return {
        "id": kind.id,
        "key": kind.key,
        "label": kind.label,
        "plural_label": kind.plural_label,
        "position": kind.position,
        "is_primary": kind.is_primary,
        "is_builtin": kind.is_builtin,
    }


def _group_json(group: Group, counts: dict[int, int] | None = None) -> dict:
    return {
        "id": group.id,
        "type_id": group.type_id,
        "name": group.name,
        "parent_id": group.parent_id,
        "lead_name": group.lead_name,
        "lead_role": group.lead_role,
        "is_active": group.is_active,
        "member_count": (counts or {}).get(group.id, 0),
    }


@router.get("/group-types")
def list_group_types(scope: TenantScope = Depends(user_scope)) -> dict:
    return {"group_types": [_type_json(t) for t in grp.types_for(scope)]}


@router.post("/group-types", status_code=status.HTTP_201_CREATED)
def create_group_type(
    payload: GroupTypeRequest,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    """Add an axis: regions, product lines, hiring cohorts.

    This is the change that used to need a release. Everything downstream —
    boards, charts, themes, permissions — reads group types rather than a
    hard-coded pair, so a new one works everywhere the moment it exists.
    """
    key = payload.key or payload.label.strip().lower().replace(" ", "_")
    try:
        kind = grp.create_type(scope, key, payload.label, payload.plural_label)
    except grp.GroupError as exc:
        raise _refuse_group(exc) from exc

    audit.record(
        scope, "group_type.create", actor_user_id=context.user.id,
        actor_label=context.user.email, target=kind.key,
    )
    scope.commit()
    return _type_json(kind)


@router.patch("/group-types/{type_id}")
def rename_group_type(
    type_id: int,
    payload: GroupTypeRequest,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    kind = scope.get(GroupType, type_id)
    if kind is None:
        raise HTTPException(status_code=404, detail="Grouping not found.")
    try:
        grp.rename_type(scope, kind, payload.label, payload.plural_label)
    except grp.GroupError as exc:
        raise _refuse_group(exc) from exc

    audit.record(
        scope, "group_type.rename", actor_user_id=context.user.id,
        actor_label=context.user.email, target=kind.key,
    )
    scope.commit()
    return _type_json(kind)


@router.post("/group-types/{type_id}/primary")
def set_primary_group_type(
    type_id: int,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    """Choose what a board groups by when a screen does not say."""
    kind = scope.get(GroupType, type_id)
    if kind is None:
        raise HTTPException(status_code=404, detail="Grouping not found.")
    grp.make_primary(scope, kind)
    audit.record(
        scope, "group_type.primary", actor_user_id=context.user.id,
        actor_label=context.user.email, target=kind.key,
    )
    scope.commit()
    return {"ok": True, "group_types": [_type_json(t) for t in grp.types_for(scope)]}


@router.post("/group-types/{type_id}/delete")
def delete_group_type(
    type_id: int,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    kind = scope.get(GroupType, type_id)
    if kind is None:
        raise HTTPException(status_code=404, detail="Grouping not found.")
    key = kind.key
    try:
        grp.delete_type(scope, kind)
    except grp.GroupError as exc:
        raise _refuse_group(exc) from exc

    audit.record(
        scope, "group_type.delete", actor_user_id=context.user.id,
        actor_label=context.user.email, target=key,
    )
    scope.commit()
    return {"ok": True}


# ---------------------------------------------------------------- groups
@router.get("/groups")
def list_groups(
    type_key: str = Query(default="", alias="type"),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    types = grp.types_for(scope)
    wanted = next((t for t in types if t.key == type_key), None) if type_key else None
    counts = grp.member_counts(scope)
    rows = grp.groups_for(scope, wanted.id if wanted else None)
    return {
        "groups": [_group_json(g, counts) for g in rows],
        "group_types": [_type_json(t) for t in types],
    }


@router.post("/groups", status_code=status.HTTP_201_CREATED)
def create_group(
    payload: GroupRequest,
    context: AuthContext = Depends(require_role(Role.branch_manager)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    kind = scope.get(GroupType, payload.type_id) if payload.type_id else grp.primary_type(scope)
    if kind is None:
        raise HTTPException(status_code=404, detail="Grouping not found.")

    parent_id = payload.parent_id
    if context.role == Role.branch_manager and context.membership.group_id:
        # A branch manager builds inside their own branch, never elsewhere.
        parent_id = context.membership.group_id

    try:
        group = grp.create_group(
            scope, kind.id, payload.name, parent_id=parent_id,
            lead_name=payload.lead_name, lead_role=payload.lead_role,
        )
    except grp.GroupError as exc:
        raise _refuse_group(exc) from exc

    audit.record(
        scope, "group.create", actor_user_id=context.user.id,
        actor_label=context.user.email, target=payload.name,
        detail={"type": kind.key},
    )
    scope.commit()
    return _group_json(group)


@router.patch("/groups/{group_id}")
def update_group(
    group_id: int,
    payload: GroupRequest,
    context: AuthContext = Depends(require_role(Role.team_lead)),
    scope: TenantScope = Depends(user_scope),
    session: Session = Depends(get_session),
) -> dict:
    group = scope.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found.")
    if not can_edit_group(session, context, group):
        raise HTTPException(status_code=403, detail="You cannot edit that group.")

    before = _group_json(group)
    # Moving a group under a different parent restructures the company, which
    # is an org-level decision even though renaming one is not.
    may_move = context.role in (Role.org_admin, Role.owner)
    try:
        grp.update_group(
            scope, group, payload.name, parent_id=payload.parent_id,
            lead_name=payload.lead_name, lead_role=payload.lead_role, move=may_move,
        )
    except grp.GroupError as exc:
        raise _refuse_group(exc) from exc

    audit.record(
        scope, "group.update", actor_user_id=context.user.id,
        actor_label=context.user.email, target=group.name,
        detail={"before": before, "after": _group_json(group)},
    )
    scope.commit()
    return _group_json(group)


class DeleteGroupRequest(BaseModel):
    """Where each member goes. A null destination returns them to Unassigned."""

    reassign: dict[int, int | None] = Field(default_factory=dict)


@router.post("/groups/{group_id}/delete")
def delete_group(
    group_id: int,
    payload: DeleteGroupRequest,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    group = scope.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found.")

    children = [g for g in scope.all(Group) if g.parent_id == group.id]
    if children:
        # Deleting a branch out from under its teams would silently orphan
        # them, and an orphan is invisible on every board that groups by
        # branch. Say so instead.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "Groups sit inside this one. Move or remove them first.",
                "children": [{"id": g.id, "name": g.name} for g in children],
            },
        )

    members = grp.members_of(scope, group.id)
    unplaced = [m for m in members if m.rep_id not in payload.reassign]
    if unplaced:
        # Deleting a group must never quietly delete or orphan people. The
        # caller is told exactly who still needs a destination.
        people = {rep.id: rep.name for rep in scope.all(Rep)}
        raise HTTPException(
            status_code=409,
            detail={
                "error": "Every member needs a destination before this group can be deleted.",
                "unassigned": [
                    {"id": m.rep_id, "name": people.get(m.rep_id, "")} for m in unplaced
                ],
            },
        )

    for membership in members:
        destination = payload.reassign.get(membership.rep_id)
        if destination is None:
            scope.delete(membership)
            continue
        target = scope.get(Group, destination)
        if target is None or target.type_id != group.type_id:
            raise HTTPException(
                status_code=404,
                detail=f"Destination group {destination} is not part of this grouping.",
            )
        membership.group_id = target.id

    audit.record(
        scope, "group.delete", actor_user_id=context.user.id,
        actor_label=context.user.email, target=group.name,
        detail={"moved": {str(k): v for k, v in payload.reassign.items()}},
    )
    scope.delete(group)
    scope.commit()
    return {"ok": True, "moved": len(members)}


# ---------------------------------------------------------------- people
@router.get("/reps")
def list_reps(scope: TenantScope = Depends(user_scope)) -> dict:
    types = grp.types_for(scope)
    assignments = grp.memberships_for(scope)
    direct = {(m.rep_id, m.type_id) for m in scope.all(GroupMembership)}
    primary = grp.primary_type(scope)
    primary_key = primary.key if primary else grp.TEAM

    reps = []
    for rep in scope.all(Rep):
        mine = assignments.get(rep.id) or {}
        placed = {
            kind.key: {
                "group_id": mine[kind.key].id,
                "name": mine[kind.key].name,
                # An inherited group — a branch reached through a team — is
                # shown but must not look like a choice somebody made here.
                "inherited": (rep.id, kind.id) not in direct,
                "source": False,
            }
            for kind in types
            if kind.key in mine
        }

        # The same fallback the board applies. Without it this page would
        # report a team as empty while the wall beside it plainly shows twelve
        # people on it — the console contradicting the product.
        for key, reported in ((grp.TEAM, rep.source_team), (grp.BRANCH, rep.home_branch)):
            if key not in placed and reported and any(k.key == key for k in types):
                placed[key] = {
                    "group_id": None, "name": reported,
                    "inherited": False, "source": True,
                }
        reps.append(
            {
                "id": rep.id,
                "rep_key": rep.rep_key,
                "name": rep.name,
                "groups": placed,
                "group": (
                    placed.get(primary_key, {}).get("name")
                    or rep.source_team
                    or "Unassigned"
                ),
                "source_team": rep.source_team,
                "assigned_locally": not (placed.get(primary_key) or {}).get("source", True),
                "home_branch": rep.home_branch,
                "title": rep.title,
            }
        )
    return {
        "reps": reps,
        "group_types": [_type_json(t) for t in types],
        "primary_type": primary_key,
    }


class AssignRequest(BaseModel):
    group_id: int | None = None
    #: Which axis is being set. Omitted means the primary grouping, so the
    #: common case — moving somebody between teams — stays a one-field call.
    type_id: int | None = None


@router.post("/reps/{rep_id}/group")
def assign_rep(
    rep_id: int,
    payload: AssignRequest,
    context: AuthContext = Depends(require_role(Role.branch_manager)),
    scope: TenantScope = Depends(user_scope),
    session: Session = Depends(get_session),
) -> dict:
    """Move a person between groups on one axis.

    This is the assignment that outranks the source system. Clearing it hands
    the person back to whatever the source reports.
    """
    rep = scope.get(Rep, rep_id)
    if rep is None:
        raise HTTPException(status_code=404, detail="Rep not found.")

    group = None
    if payload.group_id is not None:
        group = scope.get(Group, payload.group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="Group not found.")
        if not can_edit_group(session, context, group):
            raise HTTPException(status_code=403, detail="You cannot assign into that group.")
        type_id = group.type_id
    elif payload.type_id is not None:
        type_id = payload.type_id
    else:
        primary = grp.primary_type(scope)
        if primary is None:
            raise HTTPException(status_code=409, detail="This organization has no groupings.")
        type_id = primary.id

    try:
        grp.assign(scope, rep, group, type_id)
    except grp.GroupError as exc:
        raise _refuse_group(exc) from exc

    audit.record(
        scope, "rep.assign", actor_user_id=context.user.id,
        actor_label=context.user.email, target=rep.name,
        detail={"type_id": type_id, "to_group_id": payload.group_id},
    )
    scope.commit()
    return {"ok": True, "rep_id": rep.id, "group_id": payload.group_id, "type_id": type_id}


# ---------------------------------------------------------------- board preview
@router.get("/board")
def board_preview(
    mode: str = Query(default="whole_office"),
    rank_by: str = Query(default="net_split"),
    group: str = Query(default=""),
    groups: list[str] = Query(default=[]),
    group_by: str = Query(default=""),
    period_start: date | None = None,
    period_end: date | None = None,
    scope: TenantScope = Depends(user_scope),
) -> dict:
    """The same payload a television gets, for previewing before publishing."""
    mode = canonical_mode(mode)
    builder = MODES.get(mode)
    if builder is None:
        raise HTTPException(status_code=404, detail=f"Unknown mode '{mode}'.")

    period = (
        Period(start=period_start, end=period_end)
        if period_start and period_end
        else Period.current_month()
    )
    from scoreboard.services.catalogue import catalogue_for

    # The organization's catalogue, not the shipped one. Passing the default
    # meant a customer's own metric appeared on every row and then vanished
    # from the total underneath them — a board contradicting itself.
    metrics = catalogue_for(scope)
    rows = rows_for_period(scope, period, metrics=metrics, group_by=group_by)

    if mode == "per_group":
        payload = builder(rows, group, rank_by, metrics)
    elif mode == "group_vs_group":
        payload = builder(rows, groups, rank_by, metrics)
    else:
        payload = builder(rows, rank_by, metrics)

    payload["period"] = {"start": period.start.isoformat(), "end": period.end.isoformat()}
    payload["rep_count"] = len(rows)
    return payload


# ---------------------------------------------------------------- where a number came from
@router.get("/trace")
def trace_number(
    metric: str = Query(...),
    group: str = Query(default=""),
    group_by: str = Query(default=""),
    period_start: date | None = None,
    period_end: date | None = None,
    scope: TenantScope = Depends(user_scope),
) -> dict:
    """Take one figure apart into the rows that made it.

    Reruns the board's own roll-up rather than computing a second total, so an
    explanation cannot disagree with the number it claims to explain.
    """
    from scoreboard.services.catalogue import catalogue_for
    from scoreboard.services.trace import explain

    period = (
        Period(start=period_start, end=period_end)
        if period_start and period_end
        else Period.current_month()
    )
    metrics = catalogue_for(scope)
    axis = group_by or grp.primary_key(scope)
    rows = rows_for_period(scope, period, metrics=metrics, group_by=axis)

    try:
        trace = explain(rows, metric, metrics, group=group, axis=group_by)
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"'{metric}' is not a metric in this organization."
        ) from None

    return {
        **trace.as_dict(),
        "period": {"start": period.start.isoformat(), "end": period.end.isoformat()},
        "group_by": axis,
    }


@router.get("/reps/{rep_key}/evidence")
def rep_evidence(
    rep_key: str,
    period_start: date | None = None,
    period_end: date | None = None,
    scope: TenantScope = Depends(user_scope),
) -> dict:
    """What the source actually sent for one person, capture by capture."""
    from scoreboard.services.trace import evidence

    period = (
        Period(start=period_start, end=period_end)
        if period_start and period_end
        else Period.current_month()
    )
    found = evidence(scope, rep_key, period)
    if not found:
        raise HTTPException(status_code=404, detail="Nobody here by that key.")
    return found


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
    from datetime import datetime

    key = scope.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found.")
    key.revoked_at = datetime.now(UTC)
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
                "rotation": d.rotation or {},
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
    from datetime import datetime

    display = scope.get(DisplayToken, display_id)
    if display is None:
        raise HTTPException(status_code=404, detail="Display not found.")
    display.revoked_at = datetime.now(UTC)
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
    from datetime import datetime

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
        source.last_run_at = datetime.now(UTC)
        source.last_status = str(exc)[:500]
        scope.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    result = apply_records(scope, records, period)
    source.last_run_at = datetime.now(UTC)
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


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=10, max_length=200)


@router.post("/auth/password")
def change_password(
    payload: PasswordChange,
    context: AuthContext = Depends(auth_context),
    session: Session = Depends(get_session),
) -> dict:
    """Change your own password.

    Every other session of yours ends, because a password change is usually
    someone reacting to a suspicion. The session making the change survives so
    the person is not thrown out of the page they are standing on.
    """
    from scoreboard.security import hash_password, verify_password
    from scoreboard.services.auth import revoke_all_for_user

    if not verify_password(payload.current_password, context.user.password_hash):
        raise HTTPException(status_code=403, detail="Current password is incorrect.")
    if payload.new_password == payload.current_password:
        raise HTTPException(status_code=422, detail="That is the same password.")

    context.user.password_hash = hash_password(payload.new_password)
    session.commit()

    keep = context.session_row.id
    revoked = revoke_all_for_user(session, context.user.id)
    context.session_row.revoked_at = None
    session.commit()

    return {"ok": True, "other_sessions_ended": max(revoked - 1, 0), "kept_session": keep}


@router.post("/sources/{source_id}/discover")
def discover_source(
    source_id: int,
    _: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    """Ask a source what it actually returns.

    The mapping screen is filled in from this rather than from what somebody
    remembers the endpoint returning, which is how a column gets mapped to a
    field that quietly stopped existing.
    """
    source = scope.get(DataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found.")
    try:
        return _connector_for(scope, source).discover()
    except SourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
