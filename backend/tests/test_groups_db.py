"""Organization structure, against a real database.

Groups replaced two tables with one idea, and the rules that hold it together
are constraints and joins rather than functions — one group per person per
axis, a branch inferred from the team above it, a cascade that removes a
customer's structure with them. None of that can be checked without a
database, and the last time this codebase relied on the pure suite for a change
of this shape it passed a hundred and twenty-seven tests while the read path
was broken.

Skips when no database is reachable, so the pure suite still runs anywhere.
"""
from __future__ import annotations

import os
import uuid
from datetime import date

import pytest

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _reachable() -> bool:
    if not DATABASE_URL:
        return False
    try:
        from sqlalchemy import create_engine, text

        with create_engine(DATABASE_URL).connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason="needs a database; DATABASE_URL is unset or unreachable"
)


@pytest.fixture
def org():
    from scoreboard.db import session_factory
    from scoreboard.models import Organization
    from scoreboard.services import groups as grp
    from scoreboard.tenancy import TenantScope

    session = session_factory()()
    organization = Organization(name="Pytest Co", slug=f"pytest-{uuid.uuid4().hex[:10]}")
    session.add(organization)
    session.commit()

    scope = TenantScope(session, organization.id)
    grp.ensure_types(scope)
    try:
        yield scope
    finally:
        session.rollback()
        session.delete(session.get(Organization, organization.id))
        session.commit()
        session.close()


def _load(scope, rows):
    from scoreboard.connectors.base import Period, SourceRecord
    from scoreboard.services.refresh import apply_records

    records = [
        SourceRecord(rep_key=key, rep_name=name, source_team=team,
                     home_branch=branch, components=components)
        for key, name, team, branch, components in rows
    ]
    return apply_records(
        scope, records, Period(start=date(2026, 9, 1), end=date(2026, 9, 30))
    )


def _rows(scope, group_by=""):
    from scoreboard.connectors.base import Period
    from scoreboard.services.board import rows_for_period

    return rows_for_period(scope, Period.current_month(date(2026, 9, 15)), group_by=group_by)


# ---------------------------------------------------------------- types
def test_every_organization_starts_with_teams_and_branches(org):
    from scoreboard.services import groups as grp

    keys = [kind.key for kind in grp.types_for(org)]
    assert keys == ["team", "branch"]
    assert grp.primary_key(org) == "team", "boards group by team unless told otherwise"


def test_installing_the_shipped_types_twice_changes_nothing(org):
    """Called on org creation and again on first read; it must be idempotent."""
    from scoreboard.services import groups as grp

    before = [kind.id for kind in grp.types_for(org)]
    grp.ensure_types(org)
    grp.ensure_types(org)
    assert [kind.id for kind in grp.types_for(org)] == before


def test_a_shipped_grouping_cannot_be_deleted(org):
    """`branch_manager` and `team_lead` name these. Removing one orphans a rank."""
    from scoreboard.services import groups as grp

    with pytest.raises(grp.GroupError):
        grp.delete_type(org, grp.type_by_key(org, grp.TEAM))


def test_only_one_grouping_can_be_primary(org):
    """Enforced by the database, not by whichever code path writes last."""
    from sqlalchemy.exc import IntegrityError

    from scoreboard.services import groups as grp

    branch = grp.type_by_key(org, grp.BRANCH)
    branch.is_primary = True
    with pytest.raises(IntegrityError):
        org.commit()
    org.session.rollback()


def test_making_another_grouping_primary_moves_the_flag(org):
    from scoreboard.services import groups as grp

    grp.make_primary(org, grp.type_by_key(org, grp.BRANCH))
    org.commit()
    assert grp.primary_key(org) == "branch"
    assert sum(1 for kind in grp.types_for(org) if kind.is_primary) == 1


# ---------------------------------------------------------------- structure
def test_a_group_cannot_sit_inside_one_of_its_own_kind(org):
    """A team inside a branch is structure. A team inside a team is a mess."""
    from scoreboard.services import groups as grp

    team_type = grp.type_by_key(org, grp.TEAM)
    alpha = grp.create_group(org, team_type.id, "Alpha")
    with pytest.raises(grp.GroupError):
        grp.create_group(org, team_type.id, "Bravo", parent_id=alpha.id)


def test_a_group_cannot_become_its_own_ancestor(org):
    from scoreboard.services import groups as grp

    team_type = grp.type_by_key(org, grp.TEAM)
    branch_type = grp.type_by_key(org, grp.BRANCH)
    north = grp.create_group(org, branch_type.id, "North")
    alpha = grp.create_group(org, team_type.id, "Alpha", parent_id=north.id)
    org.commit()

    with pytest.raises(grp.GroupError):
        grp.update_group(org, north, "North", parent_id=alpha.id)


def test_two_groupings_may_hold_the_same_name(org):
    """"North" as a branch and "North" as a region must not collide."""
    from scoreboard.services import groups as grp

    grp.create_group(org, grp.type_by_key(org, grp.TEAM).id, "North")
    grp.create_group(org, grp.type_by_key(org, grp.BRANCH).id, "North")
    org.commit()
    assert len(grp.groups_for(org)) == 2


# ---------------------------------------------------------------- membership
def test_a_person_belongs_to_one_group_per_axis(org):
    """Reassigning replaces. Being on two teams at once is not a state."""
    from scoreboard.models import GroupMembership, Rep
    from scoreboard.services import groups as grp

    _load(org, [("a", "Ana", "", "", {"sold_leads": 1})])
    team_type = grp.type_by_key(org, grp.TEAM)
    alpha = grp.create_group(org, team_type.id, "Alpha")
    bravo = grp.create_group(org, team_type.id, "Bravo")
    rep = org.one_by(Rep, rep_key="a")

    grp.assign(org, rep, alpha, team_type.id)
    org.commit()
    grp.assign(org, rep, bravo, team_type.id)
    org.commit()

    rows = [m for m in org.all(GroupMembership) if m.rep_id == rep.id]
    assert len(rows) == 1
    assert rows[0].group_id == bravo.id


def test_a_person_can_sit_on_several_axes_at_once(org):
    from scoreboard.models import Rep
    from scoreboard.services import groups as grp

    _load(org, [("a", "Ana", "", "", {"sold_leads": 1})])
    team_type = grp.type_by_key(org, grp.TEAM)
    branch_type = grp.type_by_key(org, grp.BRANCH)
    alpha = grp.create_group(org, team_type.id, "Alpha")
    north = grp.create_group(org, branch_type.id, "North")
    rep = org.one_by(Rep, rep_key="a")

    grp.assign(org, rep, alpha, team_type.id)
    grp.assign(org, rep, north, branch_type.id)
    org.commit()

    mine = grp.memberships_for(org)[rep.id]
    assert {key: g.name for key, g in mine.items()} == {"team": "Alpha", "branch": "North"}


def test_a_branch_is_inherited_from_the_team_above(org):
    """Assigned once, true on both axes — so the two can never disagree."""
    from scoreboard.models import Rep
    from scoreboard.services import groups as grp

    _load(org, [("a", "Ana", "", "", {"sold_leads": 1})])
    team_type = grp.type_by_key(org, grp.TEAM)
    branch_type = grp.type_by_key(org, grp.BRANCH)
    north = grp.create_group(org, branch_type.id, "North")
    alpha = grp.create_group(org, team_type.id, "Alpha", parent_id=north.id)
    grp.assign(org, org.one_by(Rep, rep_key="a"), alpha, team_type.id)
    org.commit()

    assert _rows(org, "branch")[0]["group"] == "North"
    assert _rows(org, "team")[0]["group"] == "Alpha"


def test_a_direct_assignment_beats_an_inherited_one(org):
    """One person seconded to another office stays possible."""
    from scoreboard.models import Rep
    from scoreboard.services import groups as grp

    _load(org, [("a", "Ana", "", "", {"sold_leads": 1})])
    team_type = grp.type_by_key(org, grp.TEAM)
    branch_type = grp.type_by_key(org, grp.BRANCH)
    north = grp.create_group(org, branch_type.id, "North")
    south = grp.create_group(org, branch_type.id, "South")
    alpha = grp.create_group(org, team_type.id, "Alpha", parent_id=north.id)

    rep = org.one_by(Rep, rep_key="a")
    grp.assign(org, rep, alpha, team_type.id)
    grp.assign(org, rep, south, branch_type.id)
    org.commit()

    assert _rows(org, "branch")[0]["group"] == "South"


def test_a_group_cannot_take_someone_from_another_axis(org):
    from scoreboard.models import Rep
    from scoreboard.services import groups as grp

    _load(org, [("a", "Ana", "", "", {"sold_leads": 1})])
    branch_type = grp.type_by_key(org, grp.BRANCH)
    team_type = grp.type_by_key(org, grp.TEAM)
    alpha = grp.create_group(org, team_type.id, "Alpha")

    with pytest.raises(grp.GroupError):
        grp.assign(org, org.one_by(Rep, rep_key="a"), alpha, branch_type.id)


# ---------------------------------------------------------------- boards
def test_a_board_falls_back_to_what_the_source_said(org):
    """Useful before anybody has opened the group builder."""
    _load(org, [("a", "Ana", "Alpha", "Olympia", {"sold_leads": 3})])
    assert _rows(org, "team")[0]["group"] == "Alpha"
    assert _rows(org, "branch")[0]["group"] == "Olympia"


def test_an_invented_grouping_works_end_to_end(org):
    """The point of the whole change: a new axis with no code behind it.

    Nothing in this test names regions except the customer. Boards, roll-ups
    and charts read group types, so the axis works the moment it exists.
    """
    from scoreboard.domain.leaderboard import group_totals
    from scoreboard.models import Rep
    from scoreboard.services import groups as grp

    _load(org, [
        ("a", "Ana", "Alpha", "", {"sold_leads": 10, "issued_leads": 40}),
        ("b", "Boris", "Bravo", "", {"sold_leads": 30, "issued_leads": 60}),
    ])

    regions = grp.create_type(org, "region", "Region", "Regions")
    org.commit()
    west = grp.create_group(org, regions.id, "West")
    grp.assign(org, org.one_by(Rep, rep_key="a"), west, regions.id)
    grp.assign(org, org.one_by(Rep, rep_key="b"), west, regions.id)
    org.commit()

    rows = _rows(org, "region")
    assert {r["group"] for r in rows} == {"West"}

    totals = group_totals(rows, "sold_leads")
    assert totals[0]["sold_leads"] == 40
    # Derived from the region's own components, not averaged from two people.
    assert round(totals[0]["close_rate"], 4) == round(40 / 100 * 100, 4)


def test_one_board_carries_every_axis_at_once(org):
    """So a chart can break down by branch beside a board ranked by team."""
    from scoreboard.domain.leaderboard import group_totals
    from scoreboard.models import Rep
    from scoreboard.services import groups as grp

    _load(org, [
        ("a", "Ana", "", "", {"sold_leads": 10}),
        ("b", "Boris", "", "", {"sold_leads": 30}),
    ])
    team_type = grp.type_by_key(org, grp.TEAM)
    branch_type = grp.type_by_key(org, grp.BRANCH)
    north = grp.create_group(org, branch_type.id, "North")
    alpha = grp.create_group(org, team_type.id, "Alpha", parent_id=north.id)
    bravo = grp.create_group(org, team_type.id, "Bravo", parent_id=north.id)
    grp.assign(org, org.one_by(Rep, rep_key="a"), alpha, team_type.id)
    grp.assign(org, org.one_by(Rep, rep_key="b"), bravo, team_type.id)
    org.commit()

    rows = _rows(org, "team")
    assert len(group_totals(rows, "sold_leads")) == 2
    # Same rows, different axis, one query.
    by_branch = group_totals(rows, "sold_leads", axis="branch")
    assert len(by_branch) == 1
    assert by_branch[0]["sold_leads"] == 40


def test_one_organization_cannot_see_another_s_structure(org):
    from scoreboard.db import session_factory
    from scoreboard.models import Organization
    from scoreboard.services import groups as grp
    from scoreboard.tenancy import TenantScope

    grp.create_group(org, grp.type_by_key(org, grp.TEAM).id, "Alpha")
    org.commit()

    session = session_factory()()
    other = Organization(name="Other Co", slug=f"pytest-{uuid.uuid4().hex[:10]}")
    session.add(other)
    session.commit()
    try:
        stranger = TenantScope(session, other.id)
        grp.ensure_types(stranger)
        assert grp.groups_for(stranger) == []
        # And the same name is free over there.
        grp.create_group(stranger, grp.type_by_key(stranger, grp.TEAM).id, "Alpha")
        stranger.commit()
    finally:
        session.delete(session.get(Organization, other.id))
        session.commit()
        session.close()


def test_removing_a_grouping_takes_its_groups_with_it(org):
    from scoreboard.services import groups as grp

    regions = grp.create_type(org, "region", "Region", "Regions")
    org.commit()
    grp.create_group(org, regions.id, "West")
    org.commit()

    grp.delete_type(org, regions)
    org.commit()
    assert grp.type_by_key(org, "region") is None
    assert grp.groups_for(org) == []


def test_deleting_an_organization_removes_its_structure(org):
    """One delete is the whole cleanup, which the platform route relies on."""
    from scoreboard.db import session_factory
    from scoreboard.models import Group, GroupType, Organization
    from scoreboard.services import groups as grp
    from scoreboard.tenancy import TenantScope

    session = session_factory()()
    doomed = Organization(name="Doomed Co", slug=f"pytest-{uuid.uuid4().hex[:10]}")
    session.add(doomed)
    session.commit()
    org_id = doomed.id

    scope = TenantScope(session, org_id)
    grp.ensure_types(scope)
    grp.create_group(scope, grp.type_by_key(scope, grp.TEAM).id, "Alpha")
    scope.commit()

    session.delete(session.get(Organization, org_id))
    session.commit()

    assert [g for g in session.query(Group).all() if g.org_id == org_id] == []
    assert [t for t in session.query(GroupType).all() if t.org_id == org_id] == []
    session.close()
