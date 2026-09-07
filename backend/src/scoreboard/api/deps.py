"""Request-level authentication and tenant resolution.

Three separate credential types, deliberately not interchangeable:

* **API keys** identify a machine pushing data in. Write-only.
* **Display tokens** identify a television. Read-only, and they never carry
  an organization's settings — only what one screen needs to render.
* **User sessions** identify a person in the console. (Added with the auth
  router; not yet issued.)

Each resolves to a `TenantScope`, so no handler ever sees a raw session or
decides for itself which organization it is serving.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Depends, Header, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from scoreboard.db import get_session
from scoreboard.models import ApiKey, DisplayToken, Role
from scoreboard.security import TOKEN_PREFIX_LENGTH, token_matches
from scoreboard.services.auth import AuthContext, has_role, resolve_session
from scoreboard.tenancy import TenantScope


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _bearer(authorization: str) -> str:
    if not authorization.lower().startswith("bearer "):
        raise _unauthorized("Missing bearer token.")
    token = authorization[7:].strip()
    if not token:
        raise _unauthorized("Missing bearer token.")
    return token


def api_key_scope(
    authorization: str = Header(default=""),
    session: Session = Depends(get_session),
) -> TenantScope:
    token = _bearer(authorization)

    # Look up by prefix, then verify the hash. The prefix is indexed; the
    # comparison is constant time.
    candidates = session.scalars(
        select(ApiKey).where(
            ApiKey.prefix == token[:TOKEN_PREFIX_LENGTH],
            ApiKey.revoked_at.is_(None),
        )
    ).all()
    for key in candidates:
        if token_matches(token, key.token_hash):
            key.last_used_at = datetime.now(UTC)
            session.commit()
            return TenantScope(session, key.org_id)

    raise _unauthorized("Invalid or revoked API key.")


def display_scope(
    token: str = Path(...),
    session: Session = Depends(get_session),
) -> tuple[TenantScope, DisplayToken]:
    candidates = session.scalars(
        select(DisplayToken).where(
            DisplayToken.prefix == token[:TOKEN_PREFIX_LENGTH],
            DisplayToken.revoked_at.is_(None),
        )
    ).all()
    for display in candidates:
        if token_matches(token, display.token_hash):
            display.last_seen_at = datetime.now(UTC)
            session.commit()
            return TenantScope(session, display.org_id), display

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="This display link is not valid. It may have been revoked.",
    )


# ---------------------------------------------------------------- console users
def auth_context(
    authorization: str = Header(default=""),
    session: Session = Depends(get_session),
) -> AuthContext:
    context = resolve_session(session, _bearer(authorization))
    if context is None:
        raise _unauthorized("Session is invalid, expired or revoked.")
    return context


def user_scope(
    context: AuthContext = Depends(auth_context),
    session: Session = Depends(get_session),
) -> TenantScope:
    """Tenant scope for the organization this session is currently acting as.

    A platform operator holds a session that belongs nowhere, so it is refused
    here rather than defaulting to some organization. Support work inside a
    customer means being given an account there.
    """
    if context.org_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This session is not acting for any organization. "
                   "Open /admin, or grant yourself access to a company first.",
        )
    return TenantScope(session, context.org_id)


def require_role(minimum: Role):
    """Dependency factory guarding an endpoint by rank.

    Rank alone is not enough for anything group-shaped; those endpoints also
    run `can_edit_group`, because a team lead has full authority over one team
    and none at all over the next.
    """

    def guard(context: AuthContext = Depends(auth_context)) -> AuthContext:
        if not has_role(context, minimum):
            detail = (
                "This session is not acting for any organization."
                if context.membership is None
                else f"This action requires {minimum.value} or higher."
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
        return context

    return guard
