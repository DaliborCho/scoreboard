"""Platform administration: the operator's view across every customer.

This is the one part of the system that deliberately steps outside
`TenantScope`. Everything else in the codebase is written so that crossing an
organization boundary is impossible by construction; here it is the job. That
makes this file the place to be most careful, so:

* every route sits behind `superadmin_only` and nothing else reaches it;
* the flag lives on the account, not in `Role`, so the per-organization
  permission model is not quietly widened to include "and also everyone";
* nothing here reads or writes a customer's *contents* — groups, people,
  metrics. It creates, renames, suspends and counts. Looking inside a
  customer's data means being given an account there, which is auditable.
"""
from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from scoreboard.api.deps import auth_context
from scoreboard.db import get_session
from scoreboard.models import (
    ApiKey,
    DataSource,
    DisplayToken,
    Group,
    Membership,
    Organization,
    Rep,
    Role,
    Screen,
    User,
)
from scoreboard.services.auth import AuthContext, create_user, revoke_all_for_user

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])

SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}[a-z0-9]$")


def superadmin_only(context: AuthContext = Depends(auth_context)) -> AuthContext:
    """The only door into this module.

    A 404 rather than a 403: someone without the flag has no business knowing
    that a platform surface exists at all.
    """
    if not context.user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    return context


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug[:62] or "org"


class OrganizationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = ""
    # The first account for the new customer. Without one, nobody can sign in
    # and the organization is a row nobody can reach.
    owner_username: str = Field(default="", max_length=80)
    owner_email: str = Field(default="", max_length=320)
    owner_password: str = Field(default="", max_length=200)


class OrganizationUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None


def _counts(session: Session, org_id: int) -> dict:
    def count(model) -> int:
        return session.scalar(
            select(func.count()).select_from(model).where(model.org_id == org_id)
        ) or 0

    return {
        "groups": count(Group),
        "reps": count(Rep),
        "people": count(Membership),
        "displays": count(DisplayToken),
        "screens": count(Screen),
        "sources": count(DataSource),
        "api_keys": count(ApiKey),
    }


def _org_json(session: Session, org: Organization) -> dict:
    return {
        "id": org.id,
        "name": org.name,
        "slug": org.slug,
        "is_active": org.is_active,
        "created_at": org.created_at.isoformat(),
        "counts": _counts(session, org.id),
    }


@router.get("/organizations")
def list_organizations(
    _: AuthContext = Depends(superadmin_only),
    session: Session = Depends(get_session),
) -> dict:
    organizations = session.scalars(select(Organization).order_by(Organization.id)).all()
    return {"organizations": [_org_json(session, org) for org in organizations]}


@router.post("/organizations", status_code=status.HTTP_201_CREATED)
def create_organization(
    payload: OrganizationRequest,
    context: AuthContext = Depends(superadmin_only),
    session: Session = Depends(get_session),
) -> dict:
    slug = (payload.slug or slugify(payload.name)).strip().lower()
    if not SLUG_OK.match(slug):
        raise HTTPException(
            status_code=422,
            detail="Slug must be lowercase letters, digits and dashes, and start and end "
                   "with one of those.",
        )
    if session.scalars(select(Organization).where(Organization.slug == slug)).first():
        raise HTTPException(status_code=409, detail=f"'{slug}' is already taken.")

    org = Organization(name=payload.name.strip(), slug=slug)
    session.add(org)
    session.commit()

    # Give the new customer the two shipped groupings straight away. Without
    # them their first screen would have nothing to group by, and the console
    # would show an empty structure page with no way to start.
    from scoreboard.services.groups import ensure_types
    from scoreboard.tenancy import TenantScope

    ensure_types(TenantScope(session, org.id))

    handle = (payload.owner_username or "").strip().lower()
    email = (payload.owner_email or "").strip().lower() or f"owner@{slug}.invalid"
    password = payload.owner_password or secrets.token_urlsafe(9)

    if handle and session.scalars(select(User).where(User.username == handle)).first():
        raise HTTPException(status_code=409, detail=f"The handle '{handle}' is taken.")
    if session.scalars(select(User).where(User.email == email)).first():
        raise HTTPException(status_code=409, detail=f"'{email}' already has an account.")

    owner = create_user(session, email, password, full_name=f"{org.name} owner")
    owner.username = handle or None
    session.add(Membership(org_id=org.id, user_id=owner.id, role=Role.owner))
    session.commit()

    return {
        **_org_json(session, org),
        "owner": {
            "sign_in_with": handle or email,
            "password": password,
            "note": "Shown once. The account is stored hashed.",
        },
    }


@router.patch("/organizations/{org_id}")
def update_organization(
    org_id: int,
    payload: OrganizationUpdate,
    _: AuthContext = Depends(superadmin_only),
    session: Session = Depends(get_session),
) -> dict:
    org = session.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found.")

    if payload.name is not None:
        org.name = payload.name.strip() or org.name
    if payload.is_active is not None and payload.is_active != org.is_active:
        org.is_active = payload.is_active
        if not org.is_active:
            # Suspending has to take effect now. Their televisions keep working
            # — a board going dark is not what a billing dispute should look
            # like — but nobody can sign in and change anything.
            for membership in session.scalars(
                select(Membership).where(Membership.org_id == org.id)
            ).all():
                revoke_all_for_user(session, membership.user_id)

    session.commit()
    return _org_json(session, org)


class DeleteRequest(BaseModel):
    # Typing the slug back is the confirmation. A dialog that only asks "are
    # you sure" is the same click twice.
    confirm_slug: str


@router.post("/organizations/{org_id}/delete")
def delete_organization(
    org_id: int,
    payload: DeleteRequest,
    context: AuthContext = Depends(superadmin_only),
    session: Session = Depends(get_session),
) -> dict:
    org = session.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    if payload.confirm_slug.strip() != org.slug:
        raise HTTPException(
            status_code=422,
            detail=f"Type '{org.slug}' to confirm. This removes every group, person, "
                   "screen and figure belonging to them.",
        )

    counts = _counts(session, org.id)
    # Every customer table cascades from organizations, so this is one delete
    # rather than a hand-written sweep that a new table could fall out of.
    session.delete(org)
    session.commit()
    return {"ok": True, "deleted": {"name": org.name, "slug": org.slug, **counts}}


@router.get("/stats")
def platform_stats(
    _: AuthContext = Depends(superadmin_only),
    session: Session = Depends(get_session),
) -> dict:
    def total(model) -> int:
        return session.scalar(select(func.count()).select_from(model)) or 0

    return {
        "organizations": total(Organization),
        "active_organizations": session.scalar(
            select(func.count()).select_from(Organization).where(Organization.is_active.is_(True))
        ) or 0,
        "users": total(User),
        "groups": total(Group),
        "reps": total(Rep),
        "displays": total(DisplayToken),
        "screens": total(Screen),
        "sources": total(DataSource),
    }


class OperatorAccess(BaseModel):
    role: Role = Role.owner


@router.post("/organizations/{org_id}/grant-me-access")
def grant_access(
    org_id: int,
    payload: OperatorAccess,
    context: AuthContext = Depends(superadmin_only),
    session: Session = Depends(get_session),
) -> dict:
    """Give the operator a normal account inside a customer, for support.

    Deliberately explicit rather than letting the platform flag read customer
    data directly. Support work then happens as a membership that appears in
    that customer's own people list and audit log, instead of invisibly.
    """
    org = session.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found.")

    existing = session.scalars(
        select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == context.user.id
        )
    ).first()
    if existing:
        return {"ok": True, "already": True, "role": existing.role.value}

    session.add(Membership(org_id=org_id, user_id=context.user.id, role=payload.role))
    session.commit()
    return {"ok": True, "already": False, "role": payload.role.value}
