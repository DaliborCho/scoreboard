"""Multi-tenant schema.

Rule for this file: every table that holds customer data carries `org_id`.
`tests/test_tenancy.py` enforces it, so a new table cannot quietly become
cross-tenant readable.

Ownership split, inherited from the original product and kept deliberately:
the source system supplies metrics; the platform owns organization structure.
A refresh overwrites numbers and never touches group assignments.

Structure is one idea, not several. There used to be a `Team` table and a
`Branch` table, which meant a customer who also wanted to compare regions, or
product lines, or hiring cohorts, needed a third table and a release. A group
type is a row now, so those are configuration.
"""
from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from scoreboard.db import Base

# Tables that are intentionally global rather than per-organization.
GLOBAL_TABLES = {"users"}


class Role(enum.StrEnum):
    owner = "owner"
    org_admin = "org_admin"
    branch_manager = "branch_manager"
    team_lead = "team_lead"
    viewer = "viewer"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------- tenancy roots
class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # `passive_deletes` hands the work to the database's own cascade. Without
    # it SQLAlchemy tries to null `groups.org_id` first, which the NOT NULL
    # constraint refuses -- so deleting a customer would fail on their
    # structure rather than removing it.
    groups: Mapped[list[Group]] = relationship(
        back_populates="organization", passive_deletes=True
    )


class User(Base, TimestampMixin):
    """Global identity. A person may belong to several organizations."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    # A short handle to sign in with, for accounts that are not a person's
    # mailbox: the platform operator, and demo logins. Optional, because a
    # real customer signs in with the address they were invited at.
    username: Mapped[str | None] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Platform operator. Deliberately a property of the account rather than a
    # Role, because Role answers "what may you do inside one organization" and
    # this answers "may you stand outside all of them". Conflating the two
    # would make the tenant guard look like it covers a case it does not.
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Membership(Base, TimestampMixin):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_membership_org_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"), nullable=False)
    # Set for branch_manager / team_lead to scope what they may edit. One
    # column rather than two, because "which part of the company" is one
    # question: a branch manager's group is a branch, a team lead's is a team,
    # and authority reaches everything beneath it either way.
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="SET NULL"))


class UserSession(Base):
    """A signed-in person, stored server-side so it can be revoked.

    Deliberately not a JWT. When someone leaves a company, or a laptop is
    lost, the customer expects the session to stop working immediately, and a
    self-contained token cannot offer that.

    `org_id` is the organization the session is currently acting as. A person
    who belongs to several switches between them rather than juggling headers.
    """

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Null for a platform operator, who belongs to no organization. Every
    # tenant-scoped route refuses such a session outright rather than falling
    # back to "any organization", which is the mistake this nullability makes
    # visible instead of hiding.
    org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------- organization structure
class GroupType(Base, TimestampMixin):
    """One axis a company is divided along: teams, branches, regions, cohorts.

    Every organization ships with `team` and `branch`, because those are what
    the boards this replaces are actually organized by. A customer may relabel
    either, and add their own — a group type is a row, so "compare product
    lines" is a form rather than a release.
    """

    __tablename__ = "group_types"
    __table_args__ = (
        UniqueConstraint("org_id", "key", name="uq_group_type_org_key"),
        # At most one primary grouping per organization, enforced by the
        # database rather than by whichever code path happens to set it.
        Index(
            "uq_group_type_org_primary",
            "org_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))

    key: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    plural_label: Mapped[str] = mapped_column(String(80), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # The grouping a board uses when a screen does not say otherwise.
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Shipped with the product: relabel yes, delete no. Roles name these two
    # (`branch_manager`, `team_lead`), so removing them would leave a rank
    # pointing at nothing.
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    groups: Mapped[list[Group]] = relationship(back_populates="type", passive_deletes=True)


class Group(Base, TimestampMixin):
    """One team, branch, region — whatever its type says it is.

    `parent_id` is what makes a branch manager's authority mean something: a
    team sits inside a branch, and permission reaches down the chain. It also
    lets a person's branch be inferred from their team rather than stored
    twice and allowed to disagree.
    """

    __tablename__ = "groups"
    __table_args__ = (
        UniqueConstraint("org_id", "type_id", "name", name="uq_group_org_type_name"),
        Index("ix_groups_org_type", "org_id", "type_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    type_id: Mapped[int] = mapped_column(ForeignKey("group_types.id", ondelete="CASCADE"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="SET NULL"))

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    lead_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    lead_role: Mapped[str] = mapped_column(String(80), default="Sales Manager", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="groups")
    type: Mapped[GroupType] = relationship(back_populates="groups")


class GroupMembership(Base, TimestampMixin):
    """A person's place on one axis.

    `type_id` is carried here as well as on the group so that "one team per
    person, one branch per person" is a unique constraint instead of a rule
    somebody has to remember. The two must agree, which `services.groups`
    guarantees and `tests/test_groups_db.py` checks.
    """

    __tablename__ = "group_memberships"
    __table_args__ = (
        UniqueConstraint("org_id", "rep_id", "type_id", name="uq_group_membership_rep_type"),
        Index("ix_group_memberships_org_group", "org_id", "group_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    rep_id: Mapped[int] = mapped_column(ForeignKey("reps.id", ondelete="CASCADE"))
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    type_id: Mapped[int] = mapped_column(ForeignKey("group_types.id", ondelete="CASCADE"))


class Rep(Base, TimestampMixin):
    """A person as reported by the source system.

    `rep_key` is unique per organization, not globally. Two customers may use
    the same internal identifier without colliding.
    """

    __tablename__ = "reps"
    __table_args__ = (UniqueConstraint("org_id", "rep_key", name="uq_rep_org_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    rep_key: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # What the source said. Reference and fallback only.
    source_team: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    home_branch: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    hire_date: Mapped[str] = mapped_column(String(40), default="", nullable=False)

    # What we own lives in `group_memberships` and survives every refresh.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class RepMetrics(Base):
    """One row per rep, per reporting period, per calendar day.

    Refreshing during the day updates today's row; tomorrow starts a new one.
    Daily history therefore accumulates automatically, which is what trend
    charts need and what cannot be backfilled later.
    """

    __tablename__ = "rep_metrics"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "rep_id",
            "period_start",
            "period_end",
            "captured_on",
            name="uq_rep_metrics_period_day",
        ),
        Index("ix_rep_metrics_org_period", "org_id", "period_start", "period_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    rep_id: Mapped[int] = mapped_column(ForeignKey("reps.id", ondelete="CASCADE"))

    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    captured_on: Mapped[date] = mapped_column(Date, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Raw additive components only. Rates and averages are always derived.
    values: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # What the source actually said, kept so "where did 47 come from?" has an
    # answer that does not depend on the source still being reachable or still
    # saying the same thing. Never read by any arithmetic — it exists to be
    # shown to a person who does not believe a number.
    source_row: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)


# ---------------------------------------------------------------- data sources
class MetricField(Base, TimestampMixin):
    """One measurable value, as a row rather than a line of code.

    A customer whose reporting system counts something we never guessed used to
    need a release. Holding the catalogue here is what makes that a form.

    The additive/derived split survives intact, because it is the rule the whole
    product rests on: additive values are stored and summed, derived ones carry
    a formula and are recomputed at whatever level is being totalled. A customer
    can add either kind; neither can escape the arithmetic.
    """

    __tablename__ = "metric_fields"
    __table_args__ = (
        UniqueConstraint("org_id", "key", name="uq_metric_field_org_key"),
        Index("ix_metric_fields_org_position", "org_id", "position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))

    key: Mapped[str] = mapped_column(String(80), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    short_label: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    # number | currency | percent | text | system
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # additive|derived|text|system

    # Only for a derived field. Evaluation follows `position`, so a formula may
    # name a field declared before it.
    #
    # Two ways of saying the same thing, on purpose. Most derived metrics are
    # one division, and three labelled boxes are a better form for that than a
    # text field. `expression` is for the rest -- a difference, a threshold, a
    # commission that changes above a number -- and wins when it is set.
    numerator: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    denominator: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    scale: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    expression: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Shipped with the product. A customer may relabel one but not delete it,
    # because screens and stored figures already refer to it.
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class DataSource(Base, TimestampMixin):
    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40), nullable=False)  # tableau | ingest | csv | mock
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Connection + report selection + column mapping. Never compiled into code.
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Encrypted at rest. Never returned by the API.
    secret_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    refresh_seconds: Mapped[int] = mapped_column(Integer, default=900, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String(500), default="Never run", nullable=False)


# ---------------------------------------------------------------- access
class ApiKey(Base, TimestampMixin):
    """Machine credential for pushing data in. Write-only scope."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[dict] = mapped_column(JSONB, default=lambda: {"ingest": True}, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DisplayToken(Base, TimestampMixin):
    """Credential for a television. Read-only, revocable, scoped to one screen.

    A TV cannot log in, so it carries a long opaque URL instead. Revoking the
    token is how a lost or relocated screen is cut off.
    """

    __tablename__ = "display_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    screen_id: Mapped[int | None] = mapped_column(ForeignKey("screens.id", ondelete="SET NULL"))

    # Optional cycle: {"screen_ids": [1, 2], "seconds": 30}. Empty means the
    # display simply stays on `screen_id`. Rotation lives on the television
    # rather than on the screen, because the same screen is often shown on one
    # wall permanently and in a cycle on another.
    rotation: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- presentation
class Screen(Base, TimestampMixin):
    """A configured view: which mode, which metrics, which widgets."""

    __tablename__ = "screens"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mode: Mapped[str] = mapped_column(String(40), nullable=False)  # whole_office | per_group | ...
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    theme_id: Mapped[int | None] = mapped_column(ForeignKey("themes.id", ondelete="SET NULL"))


class Theme(Base, TimestampMixin):
    """Design tokens, not stylesheets.

    Users pick colors, a logo and a font from a curated set. They never supply
    CSS, so no combination can produce an unreadable screen that nobody is
    present to fix.
    """

    __tablename__ = "themes"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="org")  # org | group
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    tokens: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class Asset(Base, TimestampMixin):
    """An uploaded brand image.

    `public_key` is what appears in a URL. A television has no session and
    cannot send a token for an image, so the link has to stand on its own —
    and being random rather than sequential is what stops one customer from
    walking another's assets by counting upward.
    """

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    public_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    filename: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_org_time", "org_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_label: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
