"""groups replace teams and branches

Structure becomes one idea. `teams` and `branches` were two tables answering
two questions we had guessed; a group type is a row, so regions, product lines
and hiring cohorts are configuration rather than a release.

The data move matters more than the schema here. Every existing team and
branch becomes a group, every locally assigned rep becomes a membership, and
every foreign key that pointed at a team is repointed at the group that
replaced it. `legacy_id` carries the old identifier just long enough to do
that, and is dropped at the end so nothing downstream can start relying on it.

Revision ID: 9c41f0b7a2de
Revises: 527ae14c3697
Create Date: 2026-09-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9c41f0b7a2de"
down_revision = "527ae14c3697"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_types",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("plural_label", sa.String(length=80), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "key", name="uq_group_type_org_key"),
    )
    # One primary grouping per organization, as a constraint rather than an
    # expectation of whichever code path sets it last.
    op.create_index(
        "uq_group_type_org_primary", "group_types", ["org_id"],
        unique=True, postgresql_where=sa.text("is_primary"),
    )

    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type_id", sa.Integer(),
                  sa.ForeignKey("group_types.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", sa.Integer(),
                  sa.ForeignKey("groups.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("lead_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("lead_role", sa.String(length=80), nullable=False,
                  server_default="Sales Manager"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        # Temporary: which old row this came from, so foreign keys elsewhere
        # can be repointed by joining rather than by guessing at id order.
        sa.Column("legacy_kind", sa.String(length=10), nullable=True),
        sa.Column("legacy_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "type_id", "name", name="uq_group_org_type_name"),
    )
    op.create_index("ix_groups_org_type", "groups", ["org_id", "type_id"])

    op.create_table(
        "group_memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rep_id", sa.Integer(),
                  sa.ForeignKey("reps.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", sa.Integer(),
                  sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type_id", sa.Integer(),
                  sa.ForeignKey("group_types.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "rep_id", "type_id", name="uq_group_membership_rep_type"),
    )
    op.create_index(
        "ix_group_memberships_org_group", "group_memberships", ["org_id", "group_id"]
    )

    # ---------------------------------------------------------------- data
    # Give every existing organization the two shipped groupings.
    op.execute(
        """
        INSERT INTO group_types (org_id, key, label, plural_label, position,
                                 is_primary, is_builtin)
        SELECT id, 'team', 'Team', 'Teams', 0, true, true FROM organizations
        """
    )
    op.execute(
        """
        INSERT INTO group_types (org_id, key, label, plural_label, position,
                                 is_primary, is_builtin)
        SELECT id, 'branch', 'Branch', 'Branches', 1, false, true FROM organizations
        """
    )

    # Branches first: teams point at them, so they need to exist to be parents.
    op.execute(
        """
        INSERT INTO groups (org_id, type_id, name, lead_role, legacy_kind, legacy_id)
        SELECT b.org_id, t.id, b.name, 'Branch Manager', 'branch', b.id
        FROM branches b
        JOIN group_types t ON t.org_id = b.org_id AND t.key = 'branch'
        """
    )
    op.execute(
        """
        INSERT INTO groups (org_id, type_id, name, lead_name, lead_role, is_active,
                            parent_id, legacy_kind, legacy_id)
        SELECT tm.org_id, gt.id, tm.name, tm.lead_name, tm.lead_role, tm.is_active,
               parent.id, 'team', tm.id
        FROM teams tm
        JOIN group_types gt ON gt.org_id = tm.org_id AND gt.key = 'team'
        LEFT JOIN groups parent
               ON parent.legacy_kind = 'branch' AND parent.legacy_id = tm.branch_id
        """
    )

    # Everyone a customer had placed by hand. `reps.team_id` was exactly the
    # assignment that outranks the source system, so it is the one thing here
    # that must not be lost.
    op.execute(
        """
        INSERT INTO group_memberships (org_id, rep_id, group_id, type_id)
        SELECT r.org_id, r.id, g.id, g.type_id
        FROM reps r
        JOIN groups g ON g.legacy_kind = 'team' AND g.legacy_id = r.team_id
        WHERE r.team_id IS NOT NULL
        """
    )

    # ---------------------------------------------------------------- keys
    op.add_column("memberships", sa.Column("group_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_memberships_group", "memberships", "groups", ["group_id"], ["id"],
        ondelete="SET NULL",
    )
    # A team lead's scope was a team; a branch manager's was a branch. Both
    # become the one group they hold, and the team wins where somehow both
    # were set, because it is the narrower grant.
    op.execute(
        """
        UPDATE memberships m SET group_id = g.id
        FROM groups g
        WHERE g.legacy_kind = 'branch' AND g.legacy_id = m.branch_id
        """
    )
    op.execute(
        """
        UPDATE memberships m SET group_id = g.id
        FROM groups g
        WHERE g.legacy_kind = 'team' AND g.legacy_id = m.team_id
        """
    )
    op.drop_column("memberships", "team_id")
    op.drop_column("memberships", "branch_id")

    op.add_column("screens", sa.Column("group_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_screens_group", "screens", "groups", ["group_id"], ["id"], ondelete="CASCADE"
    )
    op.execute(
        """
        UPDATE screens s SET group_id = g.id
        FROM groups g
        WHERE g.legacy_kind = 'team' AND g.legacy_id = s.team_id
        """
    )
    op.drop_column("screens", "team_id")
    # Existing walls keep drawing: the mode names moved with the concept.
    op.execute("UPDATE screens SET mode = 'per_group' WHERE mode = 'per_team'")
    op.execute("UPDATE screens SET mode = 'group_vs_group' WHERE mode = 'team_vs_team'")
    op.execute("UPDATE screens SET mode = 'all_groups' WHERE mode = 'all_teams'")

    op.add_column("themes", sa.Column("group_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_themes_group", "themes", "groups", ["group_id"], ["id"], ondelete="CASCADE"
    )
    op.execute(
        """
        UPDATE themes t SET group_id = g.id
        FROM groups g
        WHERE g.legacy_kind = 'team' AND g.legacy_id = t.team_id
        """
    )
    op.drop_column("themes", "team_id")
    op.execute("UPDATE themes SET scope = 'group' WHERE scope = 'team'")

    # A saved column list, and any organization that had materialized the
    # shipped catalogue, still call the grouping column `team`.
    op.execute("UPDATE metric_fields SET key = 'group' WHERE key = 'team'")
    op.execute(
        """
        UPDATE screens
           SET config = jsonb_set(config, '{columns}',
                 (SELECT jsonb_agg(CASE WHEN value = '\"team\"'::jsonb
                                        THEN '\"group\"'::jsonb ELSE value END)
                    FROM jsonb_array_elements(config->'columns')))
         WHERE jsonb_typeof(config->'columns') = 'array'
        """
    )

    op.drop_column("reps", "team_id")
    op.drop_table("teams")
    op.drop_table("branches")

    # The bridge is gone once nothing needs it, so no later code can quietly
    # start depending on an identifier from a schema that no longer exists.
    op.drop_column("groups", "legacy_kind")
    op.drop_column("groups", "legacy_id")


def downgrade() -> None:
    # Deliberately not reversible. Going back would have to invent a single
    # team and branch for every group type a customer has since added, and
    # silently discarding their structure is worse than refusing.
    raise NotImplementedError(
        "Groups replaced teams and branches; there is no faithful way back."
    )
