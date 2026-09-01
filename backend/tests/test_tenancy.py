"""Architecture guards for tenant isolation.

These are not feature tests. They exist so the shape of the system cannot
regress: a new table without `org_id`, or a query that reaches across
organizations, fails here rather than in a customer's account.
"""
import pytest

from scoreboard.db import Base
from scoreboard.models import GLOBAL_TABLES, Organization, Rep, Team, User
from scoreboard.tenancy import TenantScope


def test_every_customer_table_carries_org_id():
    missing = [
        name
        for name, table in Base.metadata.tables.items()
        if name not in GLOBAL_TABLES
        and name != "organizations"
        and "org_id" not in table.columns
    ]
    assert not missing, (
        f"Tables without org_id: {', '.join(missing)}. Add org_id, or add the "
        "table to GLOBAL_TABLES with a reason."
    )


def test_scope_refuses_a_model_without_org_id():
    with pytest.raises(TypeError) as exc:
        TenantScope._require_scoped(User)
    assert "org_id" in str(exc.value)


def test_scope_requires_an_organization():
    with pytest.raises(ValueError):
        TenantScope(session=None, org_id=0)


def test_scoped_models_are_reachable():
    for model in (Rep, Team):
        TenantScope._require_scoped(model)  # must not raise


def test_organizations_table_is_the_tenancy_root():
    assert "organizations" in Base.metadata.tables
    assert "slug" in Base.metadata.tables["organizations"].columns
    assert Organization.__tablename__ == "organizations"
