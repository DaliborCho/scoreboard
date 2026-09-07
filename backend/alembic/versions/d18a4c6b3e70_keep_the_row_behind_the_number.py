"""keep the row behind the number

A board is a pile of arithmetic performed on somebody else's data, and the
first question anybody asks about a figure they dislike is where it came from.
Until now the honest answer was "add these thirteen rows up", and below that,
nothing: the payload was mapped into components and thrown away.

These two columns keep what arrived. Nothing reads them for arithmetic. They
exist so a person who does not believe a number can be shown the row it was
built from, months later, without the source system still being reachable.

Revision ID: d18a4c6b3e70
Revises: c07e5b2149af
Create Date: 2026-09-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d18a4c6b3e70"
down_revision = "c07e5b2149af"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rep_metrics",
        sa.Column("source_row", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default="{}"),
    )
    op.add_column(
        "rep_metrics",
        sa.Column("source_name", sa.String(length=200), nullable=False, server_default=""),
    )
    # Defaults dropped once the existing rows have a value, so the application
    # stays the only thing deciding what a new row contains.
    op.alter_column("rep_metrics", "source_row", server_default=None)
    op.alter_column("rep_metrics", "source_name", server_default=None)


def downgrade() -> None:
    op.drop_column("rep_metrics", "source_name")
    op.drop_column("rep_metrics", "source_row")
