"""refresh a television from the console

A wall has nobody standing beside it, so there is no way to press F5 on it.
The screen already polls; this number rides along with the answer, and the
board reloads itself when it changes.

Revision ID: e2b91f4c8a37
Revises: d18a4c6b3e70
Create Date: 2026-09-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2b91f4c8a37"
down_revision = "d18a4c6b3e70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "display_tokens",
        sa.Column("reload_nonce", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("display_tokens", "reload_nonce", server_default=None)


def downgrade() -> None:
    op.drop_column("display_tokens", "reload_nonce")
