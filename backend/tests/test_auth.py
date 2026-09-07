"""Permission rules, and a guard on how endpoints are protected.

The behaviour tests cover rank and scope. The architecture test at the bottom
is the more valuable one: it fails when someone adds a mutating endpoint and
forgets the role dependency, which is the mistake that actually happens.
"""
import pytest

from scoreboard.models import Group, Membership, Role, User, UserSession
from scoreboard.services.auth import (
    ROLE_RANK,
    AuthContext,
    can_edit_group,
    editable_group_ids,
    has_role,
    subtree,
)


def context(role: Role, *, group_id=None, org_id=1) -> AuthContext:
    return AuthContext(
        user=User(id=1, email="a@b.com", password_hash="x"),
        membership=Membership(org_id=org_id, user_id=1, role=role, group_id=group_id),
        session_row=UserSession(org_id=org_id, user_id=1, prefix="p", token_hash="h"),
    )


def group(group_id: int, *, parent_id=None, org_id=1, type_id=1) -> Group:
    return Group(
        id=group_id, org_id=org_id, type_id=type_id,
        name=f"Group {group_id}", parent_id=parent_id,
    )


class FakeSession:
    """Just enough of a session to answer "what groups does this org have".

    The scope rule now reads structure, so the branch-manager cases need
    something to read. Standing up a database for a pure permission check
    would make these tests slow enough that people stop running them.
    """

    def __init__(self, groups):
        self._groups = groups

    def scalars(self, _statement):
        return self

    def all(self):
        return list(self._groups)


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
# Branch 7 holds teams 1 and 2; branch 8 holds team 3; team 9 is loose.
STRUCTURE = [
    group(7, type_id=2), group(8, type_id=2),
    group(1, parent_id=7), group(2, parent_id=7),
    group(3, parent_id=8), group(9),
]


def test_team_lead_edits_only_their_own_group():
    lead = context(Role.team_lead, group_id=1)
    session = FakeSession(STRUCTURE)
    assert can_edit_group(session, lead, group(1, parent_id=7))
    assert not can_edit_group(session, lead, group(2, parent_id=7))


def test_branch_manager_reaches_everything_under_their_branch():
    manager = context(Role.branch_manager, group_id=7)
    session = FakeSession(STRUCTURE)
    assert can_edit_group(session, manager, group(1, parent_id=7))
    assert can_edit_group(session, manager, group(2, parent_id=7))
    assert not can_edit_group(session, manager, group(3, parent_id=8))
    assert not can_edit_group(session, manager, group(9))


def test_authority_follows_the_chain_however_deep():
    """A level added later widens a manager's reach without another column."""
    deep = [*STRUCTURE, group(10, parent_id=1), group(11, parent_id=10)]
    assert subtree(deep, 7) == {7, 1, 2, 10, 11}
    assert subtree(deep, 8) == {8, 3}


def test_a_cycle_cannot_hang_the_permission_check():
    """Nothing should create one, but a wall must not stop drawing if it does."""
    looped = [group(1, parent_id=2), group(2, parent_id=1)]
    assert subtree(looped, 1) == {1, 2}


def test_org_admin_edits_any_group_in_their_organization():
    admin = context(Role.org_admin)
    session = FakeSession(STRUCTURE)
    assert can_edit_group(session, admin, group(1, parent_id=7))
    assert can_edit_group(session, admin, group(9))


def test_no_role_reaches_across_organizations():
    """Rank must never be able to substitute for tenancy."""
    session = FakeSession(STRUCTURE)
    for role in Role:
        actor = context(role, group_id=1, org_id=1)
        assert not can_edit_group(session, actor, group(1, parent_id=7, org_id=2))


def test_viewer_edits_nothing():
    session = FakeSession(STRUCTURE)
    assert not can_edit_group(session, context(Role.viewer, group_id=1), group(1))
    assert editable_group_ids(session, context(Role.viewer, group_id=1)) == []


def test_admin_editable_groups_is_unbounded():
    # None means "all of them", which the API turns into no filter at all.
    assert editable_group_ids(None, context(Role.org_admin)) is None


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
    assert "/api/v1/groups" in paths
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
