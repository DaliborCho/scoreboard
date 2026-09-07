"""a deleted group stays deleted

A board falls back to the team the source reported when nobody has been
assigned locally. That is what makes it useful on day one -- and it meant
deleting a group removed the row while every one of its people kept arriving
with the same `source_team`, so the name was back on the wall on the next
refresh.

Taken from the original product, which hit this first and solved it the same
way: write down that the name was removed on purpose.

Revision ID: f3d0c78b1e59
Revises: e2b91f4c8a37
Create Date: 2026-09-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f3d0c78b1e59"
down_revision = "e2b91f4c8a37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "retired_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type_id", sa.Integer(),
                  sa.ForeignKey("group_types.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "type_id", "name", name="uq_retired_group_org_type_name"),
    )


def downgrade() -> None:
    op.drop_table("retired_groups")
