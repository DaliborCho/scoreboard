"""The read path, against a real database.

Every other test here is pure, which is fast and has caught real defects — but
it left one gap, and the gap bit. Threading the catalogue through
`rows_for_period` introduced a parameter named `metrics`, colliding with the
loop variable already holding a row of stored figures. Inside that loop the
catalogue silently became a database row. All 127 pure tests passed; the board
would have raised on the first request.

So this module exercises the path that actually reads and writes: build an
organization, refresh figures into it, read a board back out, and check the
numbers. It skips when no database is reachable, so the pure suite still runs
anywhere.
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
    """A throwaway organization, removed afterwards whatever happens.

    Everything customer-owned cascades from `organizations`, so one delete is
    the whole cleanup — the same property the platform delete route relies on.
    """
    from scoreboard.db import session_factory
    from scoreboard.models import Organization
    from scoreboard.tenancy import TenantScope

    session = session_factory()()
    slug = f"pytest-{uuid.uuid4().hex[:10]}"
    organization = Organization(name="Pytest Co", slug=slug)
    session.add(organization)
    session.commit()

    try:
        yield TenantScope(session, organization.id)
    finally:
        session.delete(session.get(Organization, organization.id))
        session.commit()
        session.close()


def _load(scope, rows, period_start=date(2026, 9, 1), period_end=date(2026, 9, 30)):
    from scoreboard.connectors.base import Period, SourceRecord
    from scoreboard.services.refresh import apply_records

    records = [
        SourceRecord(rep_key=key, rep_name=name, source_team=team, components=components)
        for key, name, team, components in rows
    ]
    return apply_records(scope, records, Period(start=period_start, end=period_end))


def test_a_board_reads_back_what_a_refresh_wrote(org):
    """The regression. This is the call that a shadowed name broke."""
    from scoreboard.connectors.base import Period
    from scoreboard.services.board import rows_for_period

    _load(org, [
        ("a", "Ana", "Alpha", {"issued_leads": 40, "sold_leads": 10, "net_split": 80_000}),
        ("b", "Boris", "Alpha", {"issued_leads": 60, "sold_leads": 5, "net_split": 20_000}),
    ])

    rows = rows_for_period(org, Period.current_month(date(2026, 9, 15)))
    assert len(rows) == 2

    ana = next(r for r in rows if r["rep_name"] == "Ana")
    assert ana["issued_leads"] == 40
    # Derived on the way out, never stored.
    assert ana["close_rate"] == 25.0
    assert ana["team"] == "Alpha"


def test_the_summing_rule_survives_the_database(org):
    """41 sold from 201 issued, not the average of 20% and 100%."""
    from scoreboard.connectors.base import Period
    from scoreboard.domain.leaderboard import whole_office
    from scoreboard.services.board import rows_for_period

    _load(org, [
        ("heavy", "Heavy", "Alpha", {"issued_leads": 200, "sold_leads": 40}),
        ("light", "Light", "Alpha", {"issued_leads": 1, "sold_leads": 1}),
    ])

    rows = rows_for_period(org, Period.current_month(date(2026, 9, 15)))
    board = whole_office(rows, "net_split")
    assert round(board["total"]["close_rate"], 4) == round(41 / 201 * 100, 4)


def test_a_custom_field_is_stored_and_read_like_any_other(org):
    """The point of the whole refactor, end to end.

    A metric nobody shipped is added, a source supplies it, and it comes back
    off a board correctly totalled — with its calculated companion recomputed
    from the group's components rather than averaged.
    """
    from scoreboard.connectors.base import Period
    from scoreboard.domain.leaderboard import whole_office
    from scoreboard.services import catalogue as cat
    from scoreboard.services.board import rows_for_period

    cat.create(org, cat.FieldInput(
        key="callbacks", label="Callbacks", short_label="CB", kind="number", role="additive"))
    cat.create(org, cat.FieldInput(
        key="callback_rate", label="Callback Rate", short_label="CB%", kind="percent",
        role="derived", numerator="callbacks", denominator="sold_leads", scale=100.0))

    metrics = cat.catalogue_for(org)
    assert "callbacks" in metrics.additive

    _load(org, [
        ("a", "Ana", "Alpha", {"sold_leads": 10, "callbacks": 1}),
        ("b", "Boris", "Alpha", {"sold_leads": 30, "callbacks": 9}),
    ])

    rows = rows_for_period(org, Period.current_month(date(2026, 9, 15)))
    ana = next(r for r in rows if r["rep_name"] == "Ana")
    assert ana["callbacks"] == 1
    assert ana["callback_rate"] == 10.0

    board = whole_office(rows, "sold_leads", metrics)
    # 10 callbacks out of 40 sold, not the average of 10% and 30%.
    assert board["total"]["callback_rate"] == 25.0


def test_one_organization_cannot_read_another(org):
    """Tenancy, exercised rather than asserted about in the abstract."""
    from scoreboard.connectors.base import Period
    from scoreboard.db import session_factory
    from scoreboard.models import Organization
    from scoreboard.services.board import rows_for_period
    from scoreboard.tenancy import TenantScope

    _load(org, [("a", "Ana", "Alpha", {"issued_leads": 10, "sold_leads": 2})])

    session = session_factory()()
    other = Organization(name="Other Co", slug=f"pytest-{uuid.uuid4().hex[:10]}")
    session.add(other)
    session.commit()
    try:
        stranger = TenantScope(session, other.id)
        assert rows_for_period(stranger, Period.current_month(date(2026, 9, 15))) == []
    finally:
        session.delete(session.get(Organization, other.id))
        session.commit()
        session.close()


def test_a_refresh_never_moves_someone_between_teams(org):
    """The product's first rule, checked against real stored rows."""
    from scoreboard.models import Rep, Team

    _load(org, [("a", "Ana", "Alpha", {"issued_leads": 10})])

    bravo = org.add(Team(name="Bravo"))
    org.flush()
    rep = org.one_by(Rep, rep_key="a")
    rep.team_id = bravo.id
    org.commit()

    _load(org, [("a", "Ana", "Alpha", {"issued_leads": 99})])

    rep = org.one_by(Rep, rep_key="a")
    assert rep.team_id == bravo.id, "a refresh must never rewrite a team assignment"
    assert rep.source_team == "Alpha"
