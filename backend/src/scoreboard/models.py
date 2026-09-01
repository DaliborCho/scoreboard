"""Multi-tenant schema.

Rule for this file: every table that holds customer data carries `org_id`.
`tests/test_tenancy.py` enforces it, so a new table cannot quietly become
cross-tenant readable.

Ownership split, inherited from the original product and kept deliberately:
the source system supplies metrics; the platform owns organization structure.
A refresh overwrites numbers and never touches teams or assignments.
"""
from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from scoreboard.db import Base

# Tables that are intentionally global rather than per-organization.
GLOBAL_TABLES = {"users"}


class Role(str, enum.Enum):
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

    branches: Mapped[list["Branch"]] = relationship(back_populates="organization")


class User(Base, TimestampMixin):
    """Global identity. A person may belong to several organizations."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Membership(Base, TimestampMixin):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_membership_org_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"), nullable=False)
    # Set for branch_manager / team_lead to scope what they may edit.
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))


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
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
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
class Branch(Base, TimestampMixin):
    __tablename__ = "branches"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_branch_org_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="branches")
    teams: Mapped[list["Team"]] = relationship(back_populates="branch")


class Team(Base, TimestampMixin):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_team_org_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    lead_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    lead_role: Mapped[str] = mapped_column(String(80), default="Sales Manager", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    branch: Mapped[Branch | None] = relationship(back_populates="teams")


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

    # What we own. Survives every refresh.
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"))

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


# ---------------------------------------------------------------- data sources
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
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- presentation
class Screen(Base, TimestampMixin):
    """A configured view: which mode, which metrics, which widgets."""

    __tablename__ = "screens"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mode: Mapped[str] = mapped_column(String(40), nullable=False)  # whole_office | per_team | ...
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
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
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="org")  # org | team
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    tokens: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


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
