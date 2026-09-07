"""The platform boundary.

This is the one place that deliberately steps outside `TenantScope`, so the
tests here are less about what it does and more about what cannot leak into
it: an ordinary account must never reach a platform route, and platform power
must never read as seniority inside an organization.
"""
import pytest

from scoreboard.models import Group, Membership, Role, User, UserSession
from scoreboard.services.auth import (
    AuthContext,
    can_edit_group,
    editable_group_ids,
    has_role,
)


def operator() -> AuthContext:
    """Signed in, belongs to no organization."""
    return AuthContext(
        user=User(id=1, email="admin@x", username="admin", password_hash="x", is_superadmin=True),
        membership=None,
        session_row=UserSession(org_id=None, user_id=1, prefix="p", token_hash="h"),
    )


def member(role: Role, org_id: int = 1) -> AuthContext:
    return AuthContext(
        user=User(id=2, email="a@b.com", password_hash="x"),
        membership=Membership(org_id=org_id, user_id=2, role=role),
        session_row=UserSession(org_id=org_id, user_id=2, prefix="p", token_hash="h"),
    )


# ---------------------------------------------------------------- the boundary
def test_platform_power_is_not_a_role():
    """Being the operator must not satisfy any per-organization rank check.

    If it did, `require_role` would silently become "or the operator", and
    every tenant guard in the system would be weaker than it reads.
    """
    context = operator()
    for role in Role:
        assert not has_role(context, role)


def test_an_operator_has_no_organization():
    context = operator()
    assert context.org_id is None
    assert context.role is None
    assert context.is_operator is True


def test_an_operator_cannot_edit_a_group_by_default():
    assert not can_edit_group(None, operator(), Group(id=1, org_id=1, type_id=1, name="Alpha"))
    assert editable_group_ids(None, operator()) == []


def test_an_owner_is_not_an_operator():
    """Seniority inside a customer must not reach the platform surface."""
    assert member(Role.owner).is_operator is False


def test_a_membership_makes_an_operator_ordinary_inside_that_customer():
    """Support access is a real membership, with exactly its own authority."""
    context = operator()
    context.membership = Membership(org_id=7, user_id=1, role=Role.team_lead)
    assert context.org_id == 7
    assert has_role(context, Role.team_lead)
    assert not has_role(context, Role.org_admin)


# ---------------------------------------------------------------- routes
def platform_routes():
    from test_auth import api_routes

    return [(path, methods, route) for path, methods, route in api_routes()
            if path.startswith("/api/v1/platform")]


def test_every_platform_route_is_behind_the_operator_guard():
    unguarded = []
    for path, methods, route in platform_routes():
        if "superadmin_only" not in repr(getattr(route, "dependant", None)):
            unguarded.append(f"{'/'.join(sorted(methods))} {path}")
    assert platform_routes(), "No platform routes found; the guard is checking nothing."
    assert not unguarded, f"Platform routes without the operator guard: {', '.join(unguarded)}"


def test_no_tenant_route_accidentally_lives_under_platform():
    """A route needing user_scope under /platform would be a contradiction."""
    for path, _, route in platform_routes():
        assert "user_scope" not in repr(getattr(route, "dependant", None)), path


@pytest.mark.parametrize("name", ["slugify", "SLUG_OK"])
def test_slug_helpers_exist(name):
    from scoreboard.api import platform

    assert hasattr(platform, name)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Northgate Roofing", "northgate-roofing"),
        ("  Northgate  ", "northgate"),
        ("A & B, Ltd.", "a-b-ltd"),
        ("", "org"),
        ("---", "org"),
    ],
)
def test_slugify(raw, expected):
    from scoreboard.api.platform import slugify

    assert slugify(raw) == expected
