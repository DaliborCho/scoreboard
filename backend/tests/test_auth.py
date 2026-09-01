"""Permission rules, and a guard on how endpoints are protected.

The behaviour tests cover rank and scope. The architecture test at the bottom
is the more valuable one: it fails when someone adds a mutating endpoint and
forgets the role dependency, which is the mistake that actually happens.
"""
import pytest

from scoreboard.models import Membership, Role, Team, User, UserSession
from scoreboard.services.auth import (
    ROLE_RANK,
    AuthContext,
    can_edit_team,
    editable_team_ids,
    has_role,
)


def context(role: Role, *, team_id=None, branch_id=None, org_id=1) -> AuthContext:
    return AuthContext(
        user=User(id=1, email="a@b.com", password_hash="x"),
        membership=Membership(
            org_id=org_id, user_id=1, role=role, team_id=team_id, branch_id=branch_id
        ),
        session_row=UserSession(org_id=org_id, user_id=1, prefix="p", token_hash="h"),
    )


def team(team_id: int, *, branch_id=None, org_id=1) -> Team:
    return Team(id=team_id, org_id=org_id, name=f"Team {team_id}", branch_id=branch_id)


# ---------------------------------------------------------------- rank
def test_roles_are_totally_ordered():
    assert len(set(ROLE_RANK.values())) == len(Role)
    assert ROLE_RANK[Role.owner] > ROLE_RANK[Role.org_admin] > ROLE_RANK[Role.branch_manager]
    assert ROLE_RANK[Role.branch_manager] > ROLE_RANK[Role.team_lead] > ROLE_RANK[Role.viewer]


@pytest.mark.parametrize("role", list(Role))
def test_every_role_satisfies_viewer(role):
    assert has_role(context(role), Role.viewer)


def test_only_owner_and_admin_reach_org_admin():
    assert has_role(context(Role.owner), Role.org_admin)
    assert has_role(context(Role.org_admin), Role.org_admin)
    assert not has_role(context(Role.branch_manager), Role.org_admin)
    assert not has_role(context(Role.team_lead), Role.org_admin)


# ---------------------------------------------------------------- scope
def test_team_lead_edits_only_their_own_team():
    lead = context(Role.team_lead, team_id=1)
    assert can_edit_team(None, lead, team(1))
    assert not can_edit_team(None, lead, team(2))


def test_branch_manager_edits_their_branch_only():
    manager = context(Role.branch_manager, branch_id=7)
    assert can_edit_team(None, manager, team(1, branch_id=7))
    assert not can_edit_team(None, manager, team(2, branch_id=8))
    assert not can_edit_team(None, manager, team(3, branch_id=None))


def test_org_admin_edits_any_team_in_their_organization():
    admin = context(Role.org_admin)
    assert can_edit_team(None, admin, team(1))
    assert can_edit_team(None, admin, team(2, branch_id=99))


def test_no_role_reaches_across_organizations():
    """Rank must never be able to substitute for tenancy."""
    for role in Role:
        actor = context(role, team_id=1, branch_id=7, org_id=1)
        assert not can_edit_team(None, actor, team(1, branch_id=7, org_id=2))


def test_viewer_edits_nothing():
    assert not can_edit_team(None, context(Role.viewer), team(1))
    assert editable_team_ids(None, context(Role.viewer)) == []


def test_admin_editable_teams_is_unbounded():
    # None means "all of them", which the API turns into no filter at all.
    assert editable_team_ids(None, context(Role.org_admin)) is None


# ---------------------------------------------------------------- architecture
# Endpoints that legitimately take no session: signing in, and machine or
# television credentials which authenticate by their own dependency.
UNAUTHENTICATED_ROUTES = {
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/ingest/reps"),
}


GUARD_DEPENDENCIES = (
    "auth_context",
    "user_scope",
    "guard",
    "api_key_scope",
    "display_scope",
)


def walk_routes(routes):
    """Flatten the route tree.

    Included routers are not merged into `app.routes` in current FastAPI, so
    a shallow loop sees only the docs endpoints and this guard would pass
    while checking nothing. `route.path` already carries its router's prefix.
    """
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from walk_routes(original.routes)
        else:
            yield route


def api_routes():
    from scoreboard.main import create_app

    for route in walk_routes(create_app().routes):
        methods = getattr(route, "methods", None) or set()
        methods -= {"HEAD", "OPTIONS"}
        path = getattr(route, "path", "")
        if methods and path.startswith("/api/"):
            yield path, methods, route


def test_the_guard_can_see_the_application():
    """Without this, the check below would pass on an empty list."""
    paths = {path for path, _, _ in api_routes()}
    assert "/api/v1/teams" in paths
    assert "/api/v1/auth/login" in paths
    assert len(paths) > 15


def test_every_mutating_endpoint_requires_a_credential():
    unguarded = []
    checked = 0

    for path, methods, route in api_routes():
        if not methods - {"GET"}:
            continue
        if any((method, path) in UNAUTHENTICATED_ROUTES for method in methods):
            continue
        checked += 1
        dependencies = repr(getattr(route, "dependant", None))
        if not any(name in dependencies for name in GUARD_DEPENDENCIES):
            unguarded.append(f"{'/'.join(sorted(methods))} {path}")

    assert checked, "No mutating endpoints were examined; the guard is not working."
    assert not unguarded, (
        "Endpoints that change data without a credential dependency: "
        f"{', '.join(unguarded)}. Add one, or list it in UNAUTHENTICATED_ROUTES "
        "with a reason."
    )
