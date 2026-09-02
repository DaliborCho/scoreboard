"""Create a working demo organization.

Run with:  docker compose exec api python -m scoreboard.seed

Prints an API key and a display URL once. They are stored as hashes, so this
is the only time they can be read — the same rule that will apply to a real
customer creating one in the console.
"""
from __future__ import annotations

import sys

from sqlalchemy import or_, select

from scoreboard.connectors import build
from scoreboard.connectors.base import Period
from scoreboard.db import session_factory
from scoreboard.models import (
    ApiKey,
    Branch,
    DisplayToken,
    Membership,
    Organization,
    Role,
    Team,
    User,
)
from scoreboard.security import generate_token
from scoreboard.services.auth import create_user
from scoreboard.services.refresh import apply_records
from scoreboard.tenancy import TenantScope

SLUG = "demo"

# Deliberately trivial. These exist so the installation can be looked at
# without ceremony, and the admin panel says so on every page while the host
# is localhost or a private address. Change both before this is reachable from
# anywhere else.
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin"
ADMIN_EMAIL = "admin@scoreboard.invalid"

OWNER_USERNAME = "demo"
OWNER_PASSWORD = "demo"
OWNER_EMAIL = "owner@demo-company.com"
API_KEY_NAME = "Demo ingest key"
DISPLAY_NAME = "Front office TV"
TEAMS = ["Team Alpha", "Team Bravo", "Team Charlie", "Undisputed"]


def _ensure_account(session, email: str, username: str, password: str, full_name: str) -> User:
    """Create the account, or bring an existing one back to these credentials."""
    from scoreboard.security import hash_password

    user = session.scalars(
        select(User).where(or_(User.email == email, User.username == username))
    ).first()
    if user is None:
        user = create_user(session, email, password, full_name=full_name)
        print(f"Created account '{username}'")
    else:
        user.password_hash = hash_password(password)
    user.username = username
    user.is_active = True
    session.commit()
    return user


def main() -> int:
    """Assumes `alembic upgrade head` has already run."""
    session = session_factory()()

    org = session.scalars(select(Organization).where(Organization.slug == SLUG)).first()
    if org is None:
        org = Organization(name="Demo Company", slug=SLUG)
        session.add(org)
        session.commit()
        print(f"Created organization '{org.name}' (id={org.id})")
    else:
        print(f"Using existing organization '{org.name}' (id={org.id})")

    scope = TenantScope(session, org.id)

    branch = scope.one_by(Branch, name="Olympia")
    if branch is None:
        branch = scope.add(Branch(name="Olympia"))
        scope.flush()

    for name in TEAMS:
        if scope.one_by(Team, name=name) is None:
            scope.add(Team(name=name, branch_id=branch.id, lead_name="", lead_role="Sales Manager"))
    scope.commit()

    # The platform operator stands outside every organization, so it gets no
    # membership at all.
    # The demo accounts are reset to their documented credentials on every
    # run. That is right for a seed whose whole purpose is "these are the
    # logins" — but it is exactly why this script must never be pointed at an
    # installation with real customers on it.
    admin = _ensure_account(
        session, ADMIN_EMAIL, ADMIN_USERNAME, ADMIN_PASSWORD, "Platform admin"
    )
    if not admin.is_superadmin:
        admin.is_superadmin = True
        session.commit()

    owner = _ensure_account(
        session, OWNER_EMAIL, OWNER_USERNAME, OWNER_PASSWORD, "Demo Owner"
    )
    if scope.one_by(Membership, user_id=owner.id) is None:
        scope.add(Membership(user_id=owner.id, role=Role.owner))
        scope.commit()

    # Load demo numbers through the same path a real source would use.
    period = Period.current_month()
    records = build("mock", config={"branch": "Olympia"}).fetch(period)
    result = apply_records(scope, records, period)
    print(
        f"Loaded demo data for {period.start} to {period.end}: "
        f"{result.reps_seen} reps ({result.reps_created} new)"
    )

    # Credentials are created once. Re-running the seed must not litter the
    # organization with a fresh key and another television on every pass, and
    # the existing ones genuinely cannot be shown again.
    api_token = tv_token = ""
    if scope.one_by(ApiKey, name=API_KEY_NAME) is None:
        api_token, prefix, hashed = generate_token("sb")
        scope.add(ApiKey(name=API_KEY_NAME, prefix=prefix, token_hash=hashed))
    if scope.one_by(DisplayToken, name=DISPLAY_NAME) is None:
        tv_token, prefix, hashed = generate_token("tv")
        scope.add(DisplayToken(name=DISPLAY_NAME, prefix=prefix, token_hash=hashed))
    scope.commit()

    print("\n" + "=" * 72)
    print(f"  Platform admin   http://localhost:8000/admin     {ADMIN_USERNAME} / {ADMIN_PASSWORD}")
    print(f"  Company console  http://localhost:8000/console   {OWNER_USERNAME} / {OWNER_PASSWORD}")
    if api_token:
        print(f"  Ingest API key   {api_token}")
    else:
        print(f"  Ingest API key   '{API_KEY_NAME}' already exists, stored hashed.")
    if tv_token:
        print(f"  Display URL      http://localhost:8000/tv/{tv_token}")
    else:
        print(f"  Display          '{DISPLAY_NAME}' already exists, stored hashed.")
    print("\n  Tokens are shown only on the pass that creates them.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
