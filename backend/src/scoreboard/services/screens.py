"""Assembling a screen: board, widgets and theme, in one payload.

A television asks once and draws. It does not stitch three endpoints together,
because every extra request is another thing that can be half-answered on a
wall nobody is watching.

Theme resolution follows the focus of the screen. A single-group view wears
that group's colours. The whole-office view wears the organization's, so no
one team's brand takes over a board that belongs to everybody. Multi-group
views carry each group's palette alongside its card.
"""
from __future__ import annotations

from datetime import date

from scoreboard.connectors.base import Period
from scoreboard.domain import charts
from scoreboard.domain.leaderboard import MODES, UNASSIGNED, canonical_mode
from scoreboard.domain.theme import FONTS, resolve
from scoreboard.models import Group, Screen, Theme
from scoreboard.services import groups as grp
from scoreboard.services.board import rows_for_period, trend
from scoreboard.tenancy import TenantScope

DEFAULT_COLUMNS = [
    "rank", "rep_name", "group", "issued_leads", "pitched_leads",
    "sold_leads", "close_rate", "gross_split", "net_split", "dpl",
]


def org_tokens(scope: TenantScope) -> dict:
    theme = scope.one_by(Theme, scope="org")
    return theme.tokens if theme else {}


def group_tokens(scope: TenantScope) -> dict[int, dict]:
    return {
        theme.group_id: theme.tokens
        for theme in scope.all(Theme)
        if theme.scope == "group" and theme.group_id
    }


def resolved_theme(base: dict, layer: dict | None = None) -> dict:
    tokens = resolve(base, layer)
    tokens["font_stack"] = FONTS.get(tokens.get("font"), FONTS["inter"])
    return tokens


def render(
    scope: TenantScope,
    screen: Screen | None = None,
    *,
    mode: str = "whole_office",
    period: Period | None = None,
    captured_on: date | None = None,
) -> dict:
    from scoreboard.services.catalogue import catalogue_for

    config = dict(screen.config or {}) if screen else {}
    mode = canonical_mode((screen.mode if screen else mode) or "whole_office")
    builder = MODES.get(mode)
    if builder is None:
        raise ValueError(f"Unknown mode '{mode}'.")

    period = period or Period.current_month()
    rank_by = str(config.get("rank_by") or "net_split")

    # Which axis this screen is organized along. A screen that does not say
    # uses the organization's primary grouping, so an existing wall keeps
    # meaning what it meant.
    axis = str(config.get("group_by") or "") or grp.primary_key(scope)
    rows = rows_for_period(scope, period, captured_on, group_by=axis)

    type_keys = {kind.id: kind.key for kind in grp.types_for(scope)}
    groups_by_id = {g.id: g for g in scope.all(Group)}
    # Only groups on the axis in effect can be matched by name; two axes may
    # legitimately hold a group called "North" without colliding here.
    groups_by_name = {
        g.name: g for g in groups_by_id.values() if type_keys.get(g.type_id) == axis
    }

    if mode == "per_group":
        focus = config.get("group") or config.get("team") or (
            groups_by_id[screen.group_id].name
            if screen and screen.group_id in groups_by_id
            else ""
        )
        payload = builder(rows, focus, rank_by)
    elif mode == "group_vs_group":
        chosen = list(config.get("groups") or config.get("teams") or [])
        payload = builder(rows, chosen, rank_by)
    else:
        payload = builder(rows, rank_by)

    # ------------------------------------------------------------ theme
    base = org_tokens(scope)
    overrides = group_tokens(scope)

    focus_group = None
    if mode == "per_group":
        focus_group = groups_by_name.get(payload.get("group", ""))
    theme = resolved_theme(base, overrides.get(focus_group.id) if focus_group else None)

    if mode in ("all_groups", "group_vs_group"):
        # Each card wears its own group's palette on a shared board.
        for entry in payload.get("groups", []):
            group = groups_by_name.get(entry.get("group", ""))
            entry["theme"] = resolved_theme(base, overrides.get(group.id) if group else None)

    # Every mode gets the badge and colour of each group, so a whole-office
    # table can show a crest beside each rep instead of repeating group names
    # as text. This is how the boards this replaces are actually read across a
    # room: people recognise the mark, not the word.
    payload["group_art"] = {
        name: {
            "badge_url": tokens.get("badge_url", ""),
            "primary": tokens.get("primary", theme["primary"]),
            "accent": tokens.get("accent", theme["accent"]),
        }
        for name, group in groups_by_name.items()
        for tokens in [resolve(base, overrides.get(group.id))]
        if tokens.get("badge_url") or overrides.get(group.id)
    }

    # ------------------------------------------------------------ widgets
    widget_specs = list(config.get("widgets") or [])
    needs_trend = any(w.get("type") == "trend" for w in widget_specs)
    trend_points = trend(scope, period, rank_by) if needs_trend else []

    # Widgets count the same people the board is showing. A head-to-head
    # screen that puts an office-wide total above two competing groups invites
    # exactly the wrong reading of the number.
    widget_rows = rows
    if mode == "per_group":
        focus_name = payload.get("group", "")
        widget_rows = [r for r in rows if (r.get("group") or UNASSIGNED) == focus_name]
    elif mode == "group_vs_group":
        shown = {entry.get("group") for entry in payload.get("groups", [])}
        widget_rows = [r for r in rows if (r.get("group") or UNASSIGNED) in shown]

    widgets, widget_errors = [], []
    for spec in widget_specs:
        try:
            widgets.append(charts.build(spec, widget_rows, trend_points))
        except charts.ChartError as exc:
            # Surfaced rather than dropped: a silently missing chart on a wall
            # looks like the product is broken, with nothing to explain it.
            widget_errors.append({"widget": spec, "error": str(exc)})

    metrics = catalogue_for(scope)
    by_key = metrics.by_key
    axis_type = grp.type_by_key(scope, axis)
    axis_label = axis_type.label if axis_type else "Group"

    wanted = list(config.get("columns") or DEFAULT_COLUMNS)
    columns = [c for c in wanted if c in by_key or c in ("rank", "rep_name", "group")]

    def label_for(column: str) -> str:
        if column == "group":
            return axis_label
        return by_key[column].label if column in by_key else column

    def short_for(column: str) -> str:
        if column == "group":
            return axis_label[:6].upper()
        return by_key[column].short_label if column in by_key else column[:6].upper()

    payload.update(
        {
            "screen": {
                "id": screen.id if screen else None,
                "name": screen.name if screen else "Live board",
                "title": config.get("title") or "SALES LEADERBOARD",
                "subtitle": config.get("subtitle") or "",
                "refresh_seconds": int(config.get("refresh_seconds") or 20),
            },
            "group_by": {"key": axis, "label": axis_label},
            "columns": columns,
            "column_labels": {c: label_for(c) for c in columns},
            "column_short": {c: short_for(c) for c in columns},
            "column_kinds": {
                c: (by_key[c].kind if c in by_key else "text") for c in columns
            },
            "theme": theme,
            "widgets": widgets,
            "widget_errors": widget_errors,
            "widget_scope": {"mode": mode, "reps_counted": len(widget_rows)},
            "period": {"start": period.start.isoformat(), "end": period.end.isoformat()},
            "rep_count": len(rows),
        }
    )
    return payload
