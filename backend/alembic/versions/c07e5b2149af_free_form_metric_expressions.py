"""free-form metric expressions

A derived metric was one division: numerator, denominator, scale. That covers
close rate and average sale and stops at the first customer who wants
`(sold_leads - refunds) / issued_leads * 100`.

The three columns stay, because most derived metrics really are one division
and three labelled boxes are a better form for that than a text field. This
adds the column for everything else, and it wins when it is filled in.

Revision ID: c07e5b2149af
Revises: 9c41f0b7a2de
Create Date: 2026-09-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c07e5b2149af"
down_revision = "9c41f0b7a2de"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Added with a default and then stripped of it: the table has rows, and a
    # bare NOT NULL cannot be added to those. The application stays the only
    # thing that decides what a new row contains.
    op.add_column(
        "metric_fields",
        sa.Column("expression", sa.String(length=500), nullable=False, server_default=""),
    )
    op.alter_column("metric_fields", "expression", server_default=None)


def downgrade() -> None:
    op.drop_column("metric_fields", "expression")
