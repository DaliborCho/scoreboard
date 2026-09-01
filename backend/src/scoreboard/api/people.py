"""Managing who has access.

There is no mail delivery yet, so an administrator creates the account and
hands over a first password rather than sending an invitation. That is the
honest version of this feature — a real invite flow needs email, and
pretending otherwise would leave a button that silently does nothing.

Two guards matter more than the CRUD around them. Removing someone must end
their sessions immediately, and an organization must never be left without an
owner: a customer who locks themselves out has no self-service way back in.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from scoreboard.api.deps import require_role, user_scope
from scoreboard.db import get_session
from scoreboard.models import Branch, Membership, Role, Team, User
from scoreboard.services import audit
from scoreboard.services.auth import AuthContext, create_user, revoke_all_for_user
from scoreboard.tenancy import TenantScope

router = APIRouter(prefix="/api/v1/users", tags=["people"])

admin_only = require_role(Role.org_admin)


class InviteRequest(BaseModel):
    email: EmailStr
    full_name: str = ""
    role: Role = Role.viewer
    branch_id: int | None = None
    team_id: int | None = None
    # Left blank, one is generated and shown once.
    password: str = Field(default="", max_length=200, min_length=0)


class MembershipUpdate(BaseModel):
    role: Role
    branch_id: int | None = None
    team_id: int | None = None


def _membership_json(membership: Membership, user: User) -> dict:
    return {
        "membership_id": membership.id,
        "user_id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": membership.role.value,
        "branch_id": membership.branch_id,
        "team_id": membership.team_id,
        "is_active": user.is_active,
    }


def _people(scope: TenantScope, session: Session) -> list[tuple[Membership, User]]:
    memberships = scope.all(Membership)
    if not memberships:
        return []
    users = {
        user.id: user
        for user in session.scalars(
            select(User).where(User.id.in_([m.user_id for m in memberships]))
        ).all()
    }
    return [(m, users[m.user_id]) for m in memberships if m.user_id in users]


def _owner_count(scope: TenantScope) -> int:
    return sum(1 for m in scope.all(Membership) if m.role == Role.owner)


def _check_scope(scope: TenantScope, branch_id: int | None, team_id: int | None) -> None:
    if branch_id is not None and scope.get(Branch, branch_id) is None:
        raise HTTPException(status_code=404, detail="Branch not found.")
    if team_id is not None and scope.get(Team, team_id) is None:
        raise HTTPException(status_code=404, detail="Team not found.")


@router.get("")
def list_people(
    _: AuthContext = Depends(admin_only),
    scope: TenantScope = Depends(user_scope),
    session: Session = Depends(get_session),
) -> dict:
    return {
        "people": [_membership_json(m, u) for m, u in _people(scope, session)],
        "roles": [role.value for role in Role],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def add_person(
    payload: InviteRequest,
    context: AuthContext = Depends(admin_only),
    scope: TenantScope = Depends(user_scope),
    session: Session = Depends(get_session),
) -> dict:
    """Create or attach an account, and return the first password once."""
    if payload.role == Role.owner and context.role != Role.owner:
        # Only an owner may mint another owner. An org_admin promoting itself
        # sideways into ownership is the obvious escalation path.
        raise HTTPException(status_code=403, detail="Only an owner can create another owner.")

    _check_scope(scope, payload.branch_id, payload.team_id)

    email = payload.email.strip().lower()
    user = session.scalars(select(User).where(User.email == email)).first()

    password = ""
    if user is None:
        password = payload.password or secrets.token_urlsafe(12)
        user = create_user(session, email, password, full_name=payload.full_name)
        session.commit()
    elif payload.password:
        # An existing account belongs to the person, not to this organization.
        # Adding them here must not let one customer reset another's password.
        raise HTTPException(
            status_code=409,
            detail="That account already exists. Add them without setting a password.",
        )

    if scope.one_by(Membership, user_id=user.id) is not None:
        raise HTTPException(status_code=409, detail="They already belong to this organization.")

    membership = scope.add(
        Membership(
            user_id=user.id,
            role=payload.role,
            branch_id=payload.branch_id,
            team_id=payload.team_id,
        )
    )
    scope.flush()
    audit.record(
        scope, "user.add", actor_user_id=context.user.id,
        actor_label=context.user.email, target=email,
        detail={"role": payload.role.value},
    )
    scope.commit()

    result = _membership_json(membership, user)
    if password:
        result["first_password"] = password
        result["note"] = "Shown once. Hand it over directly; it is stored hashed."
    else:
        result["note"] = "Existing account added. They sign in with their own password."
    return result


@router.patch("/{membership_id}")
def update_person(
    membership_id: int,
    payload: MembershipUpdate,
    context: AuthContext = Depends(admin_only),
    scope: TenantScope = Depends(user_scope),
    session: Session = Depends(get_session),
) -> dict:
    membership = scope.get(Membership, membership_id)
    if membership is None:
        raise HTTPException(status_code=404, detail="Person not found in this organization.")

    if payload.role == Role.owner and context.role != Role.owner:
        raise HTTPException(status_code=403, detail="Only an owner can grant ownership.")
    if membership.role == Role.owner and payload.role != Role.owner and _owner_count(scope) <= 1:
        raise HTTPException(
            status_code=409,
            detail="This is the last owner. Promote someone else first, or the "
                   "organization would be left with nobody who can.",
        )
    _check_scope(scope, payload.branch_id, payload.team_id)

    before = membership.role.value
    membership.role = payload.role
    membership.branch_id = payload.branch_id
    membership.team_id = payload.team_id

    user = session.get(User, membership.user_id)
    audit.record(
        scope, "user.role", actor_user_id=context.user.id,
        actor_label=context.user.email, target=user.email if user else str(membership.user_id),
        detail={"from": before, "to": payload.role.value},
    )
    scope.commit()

    # A demotion has to take effect now, not whenever their session expires.
    revoke_all_for_user(session, membership.user_id)
    return _membership_json(membership, user)


@router.post("/{membership_id}/remove")
def remove_person(
    membership_id: int,
    context: AuthContext = Depends(admin_only),
    scope: TenantScope = Depends(user_scope),
    session: Session = Depends(get_session),
) -> dict:
    """Remove someone from this organization and end their sessions.

    The account itself survives, because it may belong to other organizations.
    """
    membership = scope.get(Membership, membership_id)
    if membership is None:
        raise HTTPException(status_code=404, detail="Person not found in this organization.")
    if membership.role == Role.owner and _owner_count(scope) <= 1:
        raise HTTPException(status_code=409, detail="This is the last owner.")
    if membership.user_id == context.user.id:
        raise HTTPException(
            status_code=409, detail="You cannot remove your own access from here."
        )

    user = session.get(User, membership.user_id)
    audit.record(
        scope, "user.remove", actor_user_id=context.user.id,
        actor_label=context.user.email, target=user.email if user else str(membership.user_id),
    )
    scope.delete(membership)
    scope.commit()

    revoked = revoke_all_for_user(session, membership.user_id)
    return {"ok": True, "sessions_ended": revoked}
