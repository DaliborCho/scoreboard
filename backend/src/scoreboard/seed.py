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
from scoreboard.db import create_all, session_factory
from scoreboard.models import ApiKey, Branch, DisplayToken, Organization, Team
from scoreboard.security import generate_token
from scoreboard.services.refresh import apply_records
from scoreboard.tenancy import TenantScope

SLUG = "demo"
TEAMS = ["Team Alpha", "Team Bravo", "Team Charlie", "Undisputed"]


def main() -> int:
    create_all()
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

    # Load demo numbers through the same path a real source would use.
    period = Period.current_month()
    records = build("mock", config={"branch": "Olympia"}).fetch(period)
    result = apply_records(scope, records, period)
    print(
        f"Loaded demo data for {period.start} to {period.end}: "
        f"{result.reps_seen} reps ({result.reps_created} new)"
    )

    api_token, api_prefix, api_hash = generate_token("sb")
    scope.add(ApiKey(name="Demo ingest key", prefix=api_prefix, token_hash=api_hash))

    tv_token, tv_prefix, tv_hash = generate_token("tv")
    scope.add(DisplayToken(name="Front office TV", prefix=tv_prefix, token_hash=tv_hash))
    scope.commit()

    print("\n" + "=" * 72)
    print("Shown once. Store them now.\n")
    print(f"  Ingest API key   {api_token}")
    print(f"  Display URL      http://localhost:8000/api/v1/display/{tv_token}/board")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
