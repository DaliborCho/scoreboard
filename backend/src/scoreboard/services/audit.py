"""Recording who changed what.

Customers buying a leaderboard for a sales floor eventually ask why someone
moved between teams, or who revoked a television. Answering that after the
fact is impossible unless it was written down as it happened.

Kept deliberately small: an audit trail that is expensive to write does not
get written.
"""
from __future__ import annotations

from scoreboard.models import AuditLog
from scoreboard.tenancy import TenantScope


def record(
    scope: TenantScope,
    action: str,
    *,
    actor_user_id: int | None = None,
    actor_label: str = "",
    target: str = "",
    detail: dict | None = None,
) -> AuditLog:
    entry = scope.add(
        AuditLog(
            actor_user_id=actor_user_id,
            actor_label=actor_label,
            action=action,
            target=target,
            detail=detail or {},
        )
    )
    return entry
