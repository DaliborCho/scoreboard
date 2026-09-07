"""Organization structure: the axes a company is divided along.

There used to be a `Team` table and a `Branch` table. That answered the two
questions we had guessed and none of the ones customers actually ask — compare
regions, compare product lines, compare people hired this year against last.
Each of those needed a table and a release.

A group type is a row here. `team` and `branch` ship with every organization
because the roles name them, but they are instances of the idea rather than
the idea itself, and a customer can add their own.

Two rules hold this together:

*   A person belongs to at most one group per type. That is a unique
    constraint rather than a convention, which is why `type_id` is carried on
    the membership as well as on the group.
*   A group may sit inside a group of another type, and both authority and
    membership flow down that chain. Someone on Team Alpha, which sits in the
    Olympia branch, is in Olympia — inferred, never stored twice, so the two
    can never disagree.
"""
from __future__ import annotations

from dataclasses import dataclass

from scoreboard.models import Group, GroupMembership, GroupType, Rep
from scoreboard.tenancy import TenantScope

# key, label, plural, position, primary
BUILTIN_TYPES = (
    ("team", "Team", "Teams", 0, True),
    ("branch", "Branch", "Branches", 1, False),
)

TEAM = "team"
BRANCH = "branch"


class GroupError(ValueError):
    """A structure that would not hold: a cycle, a clash, a type mismatch."""


# ---------------------------------------------------------------- types
def ensure_types(scope: TenantScope) -> list[GroupType]:
    """Give an organization the two shipped axes, once.

    Idempotent. Called when an organization is created, and again from
    `types_for` so an installation that predates this table catches up on
    first use rather than needing a data migration to be re-run by hand.
    """
    existing = {row.key: row for row in scope.all(GroupType)}
    made = False
    for key, label, plural, position, primary in BUILTIN_TYPES:
        if key in existing:
            continue
        scope.add(
            GroupType(
                key=key,
                label=label,
                plural_label=plural,
                position=position,
                # Only ever claimed when nothing else holds it; the partial
                # unique index would refuse a second one anyway.
                is_primary=primary and not any(t.is_primary for t in existing.values()),
                is_builtin=True,
            )
        )
        made = True
    if made:
        scope.commit()
    return types_for(scope, install=False)


def types_for(scope: TenantScope, *, install: bool = True) -> list[GroupType]:
    rows = sorted(scope.all(GroupType), key=lambda t: (t.position, t.id))
    if not rows and install:
        return ensure_types(scope)
    return rows


def type_by_key(scope: TenantScope, key: str) -> GroupType | None:
    return next((t for t in types_for(scope) if t.key == key), None)


def primary_type(scope: TenantScope) -> GroupType | None:
    """The grouping a board uses when a screen does not name one."""
    rows = types_for(scope)
    return next((t for t in rows if t.is_primary), rows[0] if rows else None)


def primary_key(scope: TenantScope) -> str:
    kind = primary_type(scope)
    return kind.key if kind else TEAM


def create_type(scope: TenantScope, key: str, label: str, plural: str) -> GroupType:
    key = (key or "").strip().lower()
    if not key.replace("_", "").isalnum() or not key[:1].isalpha():
        raise GroupError(
            "A key must start with a letter and contain only letters, digits "
            "and underscores — it appears in board payloads and chart options."
        )
    if not (label or "").strip():
        raise GroupError("A grouping needs a name; it is what appears above the list.")
    if type_by_key(scope, key) is not None:
        raise GroupError(f"'{key}' already exists in this organization.")

    rows = types_for(scope)
    row = scope.add(
        GroupType(
            key=key,
            label=label.strip(),
            plural_label=(plural or f"{label.strip()}s").strip(),
            position=(rows[-1].position + 1) if rows else 0,
            is_primary=False,
            is_builtin=False,
        )
    )
    scope.flush()
    return row


def rename_type(scope: TenantScope, kind: GroupType, label: str, plural: str) -> GroupType:
    if not (label or "").strip():
        raise GroupError("A grouping needs a name.")
    kind.label = label.strip()
    kind.plural_label = (plural or f"{label.strip()}s").strip()
    return kind


def make_primary(scope: TenantScope, kind: GroupType) -> None:
    """Choose the grouping boards fall back to.

    Cleared everywhere first and flushed, because the database allows exactly
    one and would otherwise refuse the write depending on statement order.
    """
    for other in types_for(scope):
        if other.id != kind.id and other.is_primary:
            other.is_primary = False
    scope.flush()
    kind.is_primary = True
    scope.flush()


def delete_type(scope: TenantScope, kind: GroupType) -> None:
    if kind.is_builtin:
        raise GroupError(
            f"'{kind.label}' ships with the product — roles refer to it. "
            "It can be renamed but not removed."
        )
    if kind.is_primary:
        raise GroupError(
            "This is the grouping boards fall back to. Make another one "
            "primary first, or a board would have nothing to group by."
        )
    for group in groups_for(scope, kind.id):
        scope.delete(group)
    scope.delete(kind)


# ---------------------------------------------------------------- groups
def groups_for(scope: TenantScope, type_id: int | None = None) -> list[Group]:
    rows = scope.all(Group)
    if type_id is not None:
        rows = [row for row in rows if row.type_id == type_id]
    return sorted(rows, key=lambda g: (g.name.lower(), g.id))


def by_id(scope: TenantScope) -> dict[int, Group]:
    return {group.id: group for group in scope.all(Group)}


def ancestors(group: Group, index: dict[int, Group]) -> list[Group]:
    """Every group above this one, nearest first.

    Guarded against a cycle rather than trusting `create`/`update` to have
    prevented one, because this runs on the read path that draws a wall.
    """
    chain: list[Group] = []
    seen = {group.id}
    parent = index.get(group.parent_id) if group.parent_id else None
    while parent is not None and parent.id not in seen:
        chain.append(parent)
        seen.add(parent.id)
        parent = index.get(parent.parent_id) if parent.parent_id else None
    return chain


def subtree_ids(scope: TenantScope, group_id: int) -> set[int]:
    """This group and everything beneath it, at any depth."""
    index = by_id(scope)
    children: dict[int, list[int]] = {}
    for group in index.values():
        if group.parent_id:
            children.setdefault(group.parent_id, []).append(group.id)

    found: set[int] = set()
    queue = [group_id]
    while queue:
        current = queue.pop()
        if current in found or current not in index:
            continue
        found.add(current)
        queue.extend(children.get(current, []))
    return found


def _check_parent(scope: TenantScope, group_id: int | None,
                  type_id: int, parent_id: int | None) -> None:
    if parent_id is None:
        return
    index = by_id(scope)
    parent = index.get(parent_id)
    if parent is None:
        raise GroupError("That parent group does not exist in this organization.")
    if parent.type_id == type_id:
        raise GroupError(
            "A group sits inside a group of a different kind — a team inside a "
            "branch, not inside another team."
        )
    if group_id is not None:
        # Walking up from the proposed parent must not arrive back here.
        current: Group | None = parent
        seen = set()
        while current is not None and current.id not in seen:
            if current.id == group_id:
                raise GroupError("That would put a group inside itself.")
            seen.add(current.id)
            current = index.get(current.parent_id) if current.parent_id else None


def create_group(
    scope: TenantScope, type_id: int, name: str, *,
    parent_id: int | None = None, lead_name: str = "", lead_role: str = "Sales Manager",
) -> Group:
    name = (name or "").strip()
    if not name:
        raise GroupError("A group needs a name.")
    if any(g.name == name for g in groups_for(scope, type_id)):
        raise GroupError(f"'{name}' already exists in this grouping.")
    _check_parent(scope, None, type_id, parent_id)

    group = scope.add(
        Group(
            type_id=type_id, name=name, parent_id=parent_id,
            lead_name=lead_name, lead_role=lead_role,
        )
    )
    scope.flush()
    return group


def update_group(
    scope: TenantScope, group: Group, name: str, *,
    parent_id: int | None = None, lead_name: str = "", lead_role: str = "",
    move: bool = True,
) -> Group:
    name = (name or "").strip()
    if not name:
        raise GroupError("A group needs a name.")
    clash = next(
        (g for g in groups_for(scope, group.type_id) if g.name == name and g.id != group.id), None
    )
    if clash is not None:
        raise GroupError(f"'{name}' already exists in this grouping.")

    group.name = name
    group.lead_name = lead_name
    group.lead_role = lead_role or group.lead_role
    if move:
        _check_parent(scope, group.id, group.type_id, parent_id)
        group.parent_id = parent_id
    return group


# ---------------------------------------------------------------- membership
def assign(scope: TenantScope, rep: Rep, group: Group | None, type_id: int) -> None:
    """Put someone in a group on one axis, or take them off it.

    Replaces rather than adds: one group per type per person, which is also a
    unique constraint, so a race cannot leave someone on two teams.
    """
    if group is not None and group.type_id != type_id:
        raise GroupError("That group belongs to a different grouping.")

    existing = next(
        (m for m in scope.all(GroupMembership)
         if m.rep_id == rep.id and m.type_id == type_id),
        None,
    )
    if group is None:
        if existing is not None:
            scope.delete(existing)
        return
    if existing is not None:
        existing.group_id = group.id
        return
    scope.add(GroupMembership(rep_id=rep.id, group_id=group.id, type_id=type_id))


def memberships_for(scope: TenantScope) -> dict[int, dict[str, Group]]:
    """Every person's groups, keyed by type key, with inheritance applied.

    Somebody assigned to Team Alpha, which sits inside the Olympia branch, is
    in Olympia too. A direct assignment always beats an inherited one, so
    moving a single person out of their team's branch stays possible.
    """
    index = by_id(scope)
    types = {kind.id: kind.key for kind in types_for(scope)}

    resolved: dict[int, dict[str, Group]] = {}
    inherited: dict[int, dict[str, Group]] = {}

    for row in scope.all(GroupMembership):
        group = index.get(row.group_id)
        if group is None:
            continue
        key = types.get(group.type_id)
        if key is None:
            continue
        resolved.setdefault(row.rep_id, {})[key] = group
        for parent in ancestors(group, index):
            parent_key = types.get(parent.type_id)
            if parent_key:
                inherited.setdefault(row.rep_id, {}).setdefault(parent_key, parent)

    for rep_id, extra in inherited.items():
        direct = resolved.setdefault(rep_id, {})
        for key, group in extra.items():
            direct.setdefault(key, group)
    return resolved


def member_counts(scope: TenantScope) -> dict[int, int]:
    """Directly assigned people per group. Inheritance is not double-counted."""
    counts: dict[int, int] = {}
    for row in scope.all(GroupMembership):
        counts[row.group_id] = counts.get(row.group_id, 0) + 1
    return counts


def members_of(scope: TenantScope, group_id: int) -> list[GroupMembership]:
    return [m for m in scope.all(GroupMembership) if m.group_id == group_id]


@dataclass(frozen=True)
class TypeView:
    key: str
    label: str
    plural_label: str


def type_views(scope: TenantScope) -> list[TypeView]:
    """What a board needs to offer "group by" as a choice."""
    return [TypeView(t.key, t.label, t.plural_label) for t in types_for(scope)]
