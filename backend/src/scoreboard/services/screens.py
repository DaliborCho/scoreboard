"""Assembling a screen: board, widgets and theme, in one payload.

A television asks once and draws. It does not stitch three endpoints together,
because every extra request is another thing that can be half-answered on a
wall nobody is watching.

Theme resolution follows the focus of the screen. A single-team view wears
that team's colours. The whole-office view wears the organization's, so no one
team's brand takes over a board that belongs to everybody. Multi-team views
carry each team's palette alongside its card.
"""
from __future__ import annotations

from datetime import date

from scoreboard.connectors.base import Period
from scoreboard.domain import charts
from scoreboard.domain.leaderboard import MODES, UNASSIGNED
from scoreboard.domain.metrics import METRIC_BY_KEY
from scoreboard.domain.theme import FONTS, resolve
from scoreboard.models import Screen, Team, Theme
from scoreboard.services.board import rows_for_period, trend
from scoreboard.tenancy import TenantScope

DEFAULT_COLUMNS = [
    "rank", "rep_name", "team", "issued_leads", "pitched_leads",
    "sold_leads", "close_rate", "gross_split", "net_split", "dpl",
]


def org_tokens(scope: TenantScope) -> dict:
    theme = scope.one_by(Theme, scope="org")
    return theme.tokens if theme else {}


def team_tokens(scope: TenantScope) -> dict[int, dict]:
    return {
        theme.team_id: theme.tokens
        for theme in scope.all(Theme)
        if theme.scope == "team" and theme.team_id
    }


def resolved_theme(base: dict, team_layer: dict | None = None) -> dict:
    tokens = resolve(base, team_layer)
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
    config = dict(screen.config or {}) if screen else {}
    mode = (screen.mode if screen else mode) or "whole_office"
    builder = MODES.get(mode)
    if builder is None:
        raise ValueError(f"Unknown mode '{mode}'.")

    period = period or Period.current_month()
    rank_by = str(config.get("rank_by") or "net_split")
    rows = rows_for_period(scope, period, captured_on)

    teams_by_id = {team.id: team for team in scope.all(Team)}
    teams_by_name = {team.name: team for team in teams_by_id.values()}

    if mode == "per_team":
        focus = config.get("team") or (
            teams_by_id[screen.team_id].name if screen and screen.team_id in teams_by_id else ""
        )
        payload = builder(rows, focus, rank_by)
    elif mode == "team_vs_team":
        payload = builder(rows, list(config.get("teams") or []), rank_by)
    else:
        payload = builder(rows, rank_by)

    # ------------------------------------------------------------ theme
    base = org_tokens(scope)
    overrides = team_tokens(scope)

    focus_team = None
    if mode == "per_team":
        focus_team = teams_by_name.get(payload.get("team", ""))
    theme = resolved_theme(base, overrides.get(focus_team.id) if focus_team else None)

    if mode in ("all_teams", "team_vs_team"):
        # Each card wears its own team's palette on a shared board.
        for entry in payload.get("teams", []):
            team = teams_by_name.get(entry.get("team", ""))
            entry["theme"] = resolved_theme(base, overrides.get(team.id) if team else None)

    # ------------------------------------------------------------ widgets
    widget_specs = list(config.get("widgets") or [])
    needs_trend = any(w.get("type") == "trend" for w in widget_specs)
    trend_points = trend(scope, period, rank_by) if needs_trend else []

    # Widgets count the same people the board is showing. A head-to-head
    # screen that puts an office-wide total above two competing teams invites
    # exactly the wrong reading of the number.
    widget_rows = rows
    if mode == "per_team":
        focus_name = payload.get("team", "")
        widget_rows = [r for r in rows if (r.get("team") or UNASSIGNED) == focus_name]
    elif mode == "team_vs_team":
        shown = {entry.get("team") for entry in payload.get("teams", [])}
        widget_rows = [r for r in rows if (r.get("team") or UNASSIGNED) in shown]

    widgets, widget_errors = [], []
    for spec in widget_specs:
        try:
            widgets.append(charts.build(spec, widget_rows, trend_points))
        except charts.ChartError as exc:
            # Surfaced rather than dropped: a silently missing chart on a wall
            # looks like the product is broken, with nothing to explain it.
            widget_errors.append({"widget": spec, "error": str(exc)})

    columns = [c for c in (config.get("columns") or DEFAULT_COLUMNS) if c in METRIC_BY_KEY]

    payload.update(
        {
            "screen": {
                "id": screen.id if screen else None,
                "name": screen.name if screen else "Live board",
                "title": config.get("title") or "SALES LEADERBOARD",
                "subtitle": config.get("subtitle") or "",
                "refresh_seconds": int(config.get("refresh_seconds") or 20),
            },
            "columns": columns,
            "column_labels": {c: METRIC_BY_KEY[c].label for c in columns},
            "column_short": {c: METRIC_BY_KEY[c].short_label for c in columns},
            "column_kinds": {c: METRIC_BY_KEY[c].kind for c in columns},
            "theme": theme,
            "widgets": widgets,
            "widget_errors": widget_errors,
            "widget_scope": {"mode": mode, "reps_counted": len(widget_rows)},
            "period": {"start": period.start.isoformat(), "end": period.end.isoformat()},
            "rep_count": len(rows),
        }
    )
    return payload
