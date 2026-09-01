"""display rotation

Revision ID: bc3be954a389
Revises: 8b67da093b8c
Create Date: 2026-09-01 19:56:17.973282
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "bc3be954a389"
down_revision = "8b67da093b8c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Autogenerate produced a bare NOT NULL column, which cannot be added to a
    # table that already has rows. The server default fills existing
    # televisions with "no rotation", and is then dropped so the application
    # stays the only thing that decides what a new row contains.
    op.add_column(
        "display_tokens",
        sa.Column(
            "rotation",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("display_tokens", "rotation", server_default=None)


def downgrade() -> None:
    op.drop_column("display_tokens", "rotation")
