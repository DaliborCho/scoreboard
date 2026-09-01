"""Create a working demo organization.

Run with:  docker compose exec api python -m scoreboard.seed

Prints an API key and a display URL once. They are stored as hashes, so this
is the only time they can be read — the same rule that will apply to a real
customer creating one in the console.
"""
from __future__ import annotations

import sys

from sqlalchemy import select

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
OWNER_EMAIL = "owner@demo-company.com"
OWNER_PASSWORD = "demo-password-change-me"
API_KEY_NAME = "Demo ingest key"
DISPLAY_NAME = "Front office TV"
TEAMS = ["Team Alpha", "Team Bravo", "Team Charlie", "Undisputed"]


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

    owner = session.scalars(select(User).where(User.email == OWNER_EMAIL)).first()
    if owner is None:
        owner = create_user(session, OWNER_EMAIL, OWNER_PASSWORD, full_name="Demo Owner")
        session.commit()
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
    print(f"  Console sign-in  {OWNER_EMAIL} / {OWNER_PASSWORD}")
    print("  Console          http://localhost:8000/console")
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
