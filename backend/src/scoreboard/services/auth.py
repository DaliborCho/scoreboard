"""Sign-in, server-side sessions, and what each role is allowed to do.

Sessions are rows rather than self-contained tokens. A customer who removes
someone from their organization expects that person to lose access now, and a
JWT cannot be withdrawn before it expires.

Permissions are a rank plus two scope checks. The rank answers "how senior",
the scope answers "over which part of the company" — a team lead outranks
nobody outside their own team, which is exactly the shape the product needs
once a customer has more than one office.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from scoreboard.models import Membership, Organization, Role, Team, User, UserSession
from scoreboard.security import (
    TOKEN_PREFIX_LENGTH,
    generate_token,
    hash_password,
    token_matches,
    verify_password,
)

SESSION_TTL = timedelta(days=14)

# Higher outranks lower. Comparisons are always ">=", never equality, so a new
# role inserted in the middle does not silently widen anyone's access.
ROLE_RANK: dict[Role, int] = {
    Role.viewer: 0,
    Role.team_lead: 1,
    Role.branch_manager: 2,
    Role.org_admin: 3,
    Role.owner: 4,
}


@dataclass
class AuthContext:
    """Who is acting, in which organization, with what authority."""

    user: User
    membership: Membership
    session_row: UserSession

    @property
    def org_id(self) -> int:
        return self.membership.org_id

    @property
    def role(self) -> Role:
        return self.membership.role


# ---------------------------------------------------------------- accounts
def create_user(session: Session, email: str, password: str, full_name: str = "") -> User:
    user = User(
        email=email.strip().lower(),
        password_hash=hash_password(password),
        full_name=full_name,
    )
    session.add(user)
    session.flush()
    return user


def authenticate(session: Session, email: str, password: str) -> User | None:
    user = session.scalars(
        select(User).where(User.email == email.strip().lower(), User.is_active.is_(True))
    ).first()
    if user is None:
        # Hash anyway so a missing account and a wrong password take a similar
        # amount of time and cannot be told apart by measuring.
        hash_password(password)
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def memberships_for(session: Session, user: User) -> list[Membership]:
    return list(
        session.scalars(select(Membership).where(Membership.user_id == user.id)).all()
    )


# ---------------------------------------------------------------- sessions
def start_session(
    session: Session, user: User, org_id: int, ttl: timedelta = SESSION_TTL
) -> tuple[str, UserSession]:
    token, prefix, hashed = generate_token("ss")
    row = UserSession(
        org_id=org_id,
        user_id=user.id,
        prefix=prefix,
        token_hash=hashed,
        expires_at=datetime.now(UTC) + ttl,
    )
    session.add(row)
    session.commit()
    return token, row


def resolve_session(session: Session, token: str) -> AuthContext | None:
    """Turn a bearer token into an acting identity, or nothing.

    Every reason to reject — unknown, revoked, expired, membership withdrawn —
    returns the same empty result. The caller cannot learn which.
    """
    if not token:
        return None

    now = datetime.now(UTC)
    candidates = session.scalars(
        select(UserSession).where(
            UserSession.prefix == token[:TOKEN_PREFIX_LENGTH],
            UserSession.revoked_at.is_(None),
        )
    ).all()

    for row in candidates:
        if not token_matches(token, row.token_hash):
            continue
        if row.expires_at <= now:
            return None

        user = session.get(User, row.user_id)
        if user is None or not user.is_active:
            return None

        membership = session.scalars(
            select(Membership).where(
                Membership.user_id == row.user_id, Membership.org_id == row.org_id
            )
        ).first()
        if membership is None:
            # Removed from the organization since signing in.
            return None

        row.last_seen_at = now
        session.commit()
        return AuthContext(user=user, membership=membership, session_row=row)

    return None


def revoke_session(session: Session, row: UserSession) -> None:
    row.revoked_at = datetime.now(UTC)
    session.commit()


def revoke_all_for_user(session: Session, user_id: int) -> int:
    """Used when a password changes or an account is disabled."""
    rows = session.scalars(
        select(UserSession).where(
            UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
        )
    ).all()
    now = datetime.now(UTC)
    for row in rows:
        row.revoked_at = now
    session.commit()
    return len(rows)


def switch_organization(session: Session, context: AuthContext, org_id: int) -> AuthContext | None:
    """Point an existing session at another organization the person belongs to."""
    membership = session.scalars(
        select(Membership).where(
            Membership.user_id == context.user.id, Membership.org_id == org_id
        )
    ).first()
    if membership is None:
        return None

    org = session.get(Organization, org_id)
    if org is None or not org.is_active:
        return None

    context.session_row.org_id = org_id
    session.commit()
    return AuthContext(
        user=context.user, membership=membership, session_row=context.session_row
    )


# ---------------------------------------------------------------- permissions
def has_role(context: AuthContext, minimum: Role) -> bool:
    return ROLE_RANK[context.role] >= ROLE_RANK[minimum]


def can_edit_team(session: Session, context: AuthContext, team: Team) -> bool:
    """Scope check for team-level editing, on top of the rank check.

    This is what lets a customer hand a team lead their own logo and colours
    without handing them everyone else's.
    """
    if team.org_id != context.org_id:
        return False
    if has_role(context, Role.org_admin):
        return True
    if context.role == Role.branch_manager:
        return context.membership.branch_id is not None and (
            team.branch_id == context.membership.branch_id
        )
    if context.role == Role.team_lead:
        return context.membership.team_id == team.id
    return False


def editable_team_ids(session: Session, context: AuthContext) -> list[int] | None:
    """Team ids this person may edit, or None meaning "all of them"."""
    if has_role(context, Role.org_admin):
        return None
    if context.role == Role.branch_manager and context.membership.branch_id:
        return [
            team.id
            for team in session.scalars(
                select(Team).where(
                    Team.org_id == context.org_id,
                    Team.branch_id == context.membership.branch_id,
                )
            ).all()
        ]
    if context.role == Role.team_lead and context.membership.team_id:
        return [context.membership.team_id]
    return []
