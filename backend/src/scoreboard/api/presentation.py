"""Presentation routes: themes, screens and what a television is showing.

Split from the console routes because this is the half a customer touches
constantly once they are set up, and because theming carries its own rule —
a theme that would be unreadable is refused rather than saved with a warning.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from scoreboard.api.deps import auth_context, require_role, user_scope
from scoreboard.db import get_session
from scoreboard.domain import charts
from scoreboard.domain import theme as theme_domain
from scoreboard.domain.leaderboard import MODES
from scoreboard.domain.theme import resolve
from scoreboard.models import DisplayToken, Role, Screen, Team, Theme
from scoreboard.security import generate_token
from scoreboard.services import audit
from scoreboard.services.auth import AuthContext, can_edit_team
from scoreboard.services.screens import render
from scoreboard.tenancy import TenantScope

router = APIRouter(prefix="/api/v1", tags=["presentation"])


# ---------------------------------------------------------------- themes
class ThemeRequest(BaseModel):
    name: str = "Theme"
    tokens: dict = Field(default_factory=dict)


@router.get("/themes/catalogue")
def theme_catalogue(_: AuthContext = Depends(auth_context)) -> dict:
    """Every option the editor may offer. The UI never invents one of its own."""
    return theme_domain.catalogue()


@router.post("/themes/preview")
def preview_theme(payload: ThemeRequest, _: AuthContext = Depends(auth_context)) -> dict:
    """Live contrast feedback while someone is still choosing colours."""
    return {
        **theme_domain.readability_report(payload.tokens),
        "problems": [p.as_dict() for p in theme_domain.validate(payload.tokens)],
        "resolved": theme_domain.resolve(payload.tokens),
    }


@router.get("/themes")
def list_themes(scope: TenantScope = Depends(user_scope)) -> dict:
    return {
        "themes": [
            {"id": t.id, "scope": t.scope, "team_id": t.team_id,
             "name": t.name, "tokens": t.tokens}
            for t in scope.all(Theme)
        ]
    }


def _save_theme(
    scope: TenantScope,
    context: AuthContext,
    payload: ThemeRequest,
    *,
    theme_scope: str,
    team_id: int | None,
) -> dict:
    problems = theme_domain.validate(payload.tokens)
    if problems:
        # Refused, not warned about. Nobody stands beside a television to
        # notice that the text has gone unreadable.
        raise HTTPException(
            status_code=422,
            detail={
                "error": "This theme would be unreadable on a screen.",
                "problems": [p.as_dict() for p in problems],
            },
        )

    existing = scope.one_by(Theme, scope=theme_scope, team_id=team_id)
    if existing is None:
        existing = scope.add(
            Theme(scope=theme_scope, team_id=team_id, name=payload.name, tokens=payload.tokens)
        )
    else:
        existing.name = payload.name
        existing.tokens = payload.tokens

    scope.flush()
    audit.record(
        scope, f"theme.{theme_scope}.save", actor_user_id=context.user.id,
        actor_label=context.user.email, target=payload.name,
    )
    scope.commit()
    return {
        "id": existing.id,
        "scope": existing.scope,
        "team_id": existing.team_id,
        "tokens": existing.tokens,
        "readability": theme_domain.readability_report(existing.tokens),
    }


@router.put("/themes/org")
def save_org_theme(
    payload: ThemeRequest,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    return _save_theme(scope, context, payload, theme_scope="org", team_id=None)


@router.put("/themes/team/{team_id}")
def save_team_theme(
    team_id: int,
    payload: ThemeRequest,
    context: AuthContext = Depends(require_role(Role.team_lead)),
    scope: TenantScope = Depends(user_scope),
    session: Session = Depends(get_session),
) -> dict:
    """A team lead styles their own team and nobody else's."""
    team = scope.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found.")
    if not can_edit_team(session, context, team):
        raise HTTPException(status_code=403, detail="You cannot theme that team.")
    return _save_theme(scope, context, payload, theme_scope="team", team_id=team_id)


# ---------------------------------------------------------------- screens
@router.get("/charts/catalogue")
def chart_catalogue(_: AuthContext = Depends(auth_context)) -> dict:
    return charts.catalogue()


class ScreenRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    mode: str = "whole_office"
    team_id: int | None = None
    config: dict = Field(default_factory=dict)


def _screen_json(screen: Screen) -> dict:
    return {
        "id": screen.id,
        "name": screen.name,
        "mode": screen.mode,
        "team_id": screen.team_id,
        "config": screen.config,
    }


def _validate_screen(payload: ScreenRequest) -> None:
    if payload.mode not in MODES:
        raise HTTPException(
            status_code=422, detail=f"Unknown mode. Choose one of: {', '.join(MODES)}."
        )
    problems = {}
    for index, widget in enumerate(payload.config.get("widgets") or []):
        found = charts.validate_widget(widget)
        if found:
            problems[str(index)] = found
    if problems:
        raise HTTPException(status_code=422, detail={"widgets": problems})


@router.get("/screens")
def list_screens(scope: TenantScope = Depends(user_scope)) -> dict:
    return {"screens": [_screen_json(s) for s in scope.all(Screen)]}


@router.post("/screens", status_code=status.HTTP_201_CREATED)
def create_screen(
    payload: ScreenRequest,
    context: AuthContext = Depends(require_role(Role.branch_manager)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    _validate_screen(payload)
    screen = scope.add(
        Screen(
            name=payload.name,
            mode=payload.mode,
            team_id=payload.team_id,
            config=payload.config,
        )
    )
    scope.flush()
    audit.record(
        scope, "screen.create", actor_user_id=context.user.id,
        actor_label=context.user.email, target=payload.name,
    )
    scope.commit()
    return _screen_json(screen)


@router.patch("/screens/{screen_id}")
def update_screen(
    screen_id: int,
    payload: ScreenRequest,
    context: AuthContext = Depends(require_role(Role.branch_manager)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    screen = scope.get(Screen, screen_id)
    if screen is None:
        raise HTTPException(status_code=404, detail="Screen not found.")
    _validate_screen(payload)
    screen.name = payload.name
    screen.mode = payload.mode
    screen.team_id = payload.team_id
    screen.config = payload.config
    audit.record(
        scope, "screen.update", actor_user_id=context.user.id,
        actor_label=context.user.email, target=screen.name,
    )
    scope.commit()
    return _screen_json(screen)


@router.post("/screens/{screen_id}/delete")
def delete_screen(
    screen_id: int,
    context: AuthContext = Depends(require_role(Role.branch_manager)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    screen = scope.get(Screen, screen_id)
    if screen is None:
        raise HTTPException(status_code=404, detail="Screen not found.")

    attached = [
        d.name for d in scope.all(DisplayToken)
        if d.screen_id == screen.id and d.revoked_at is None
    ]
    if attached:
        # Deleting would blank a wall in an office somewhere. Say which.
        raise HTTPException(
            status_code=409,
            detail={"error": "Televisions are still showing this screen.", "displays": attached},
        )

    audit.record(
        scope, "screen.delete", actor_user_id=context.user.id,
        actor_label=context.user.email, target=screen.name,
    )
    scope.delete(screen)
    scope.commit()
    return {"ok": True}


@router.get("/screens/{screen_id}/render")
def render_screen(screen_id: int, scope: TenantScope = Depends(user_scope)) -> dict:
    """Exactly what a television would receive, for previewing first."""
    screen = scope.get(Screen, screen_id)
    if screen is None:
        raise HTTPException(status_code=404, detail="Screen not found.")
    return render(scope, screen)


class AttachRequest(BaseModel):
    screen_id: int | None = None


@router.post("/display-tokens/{display_id}/screen")
def attach_screen(
    display_id: int,
    payload: AttachRequest,
    context: AuthContext = Depends(require_role(Role.branch_manager)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    display = scope.get(DisplayToken, display_id)
    if display is None:
        raise HTTPException(status_code=404, detail="Display not found.")
    if payload.screen_id is not None and scope.get(Screen, payload.screen_id) is None:
        raise HTTPException(status_code=404, detail="Screen not found.")

    display.screen_id = payload.screen_id
    audit.record(
        scope, "display_token.attach", actor_user_id=context.user.id,
        actor_label=context.user.email, target=display.name,
        detail={"screen_id": payload.screen_id},
    )
    scope.commit()
    return {"ok": True, "display_id": display.id, "screen_id": display.screen_id}


class RotationRequest(BaseModel):
    screen_ids: list[int] = Field(default_factory=list, max_length=12)
    seconds: int = Field(default=30, ge=5, le=3600)


@router.post("/display-tokens/{display_id}/rotation")
def set_rotation(
    display_id: int,
    payload: RotationRequest,
    context: AuthContext = Depends(require_role(Role.branch_manager)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    """Cycle a television through several screens.

    An empty list clears the cycle and the display goes back to sitting on one
    screen. Every id is checked now rather than at render time, so a deleted
    screen cannot leave a wall showing an error at 6am.
    """
    display = scope.get(DisplayToken, display_id)
    if display is None:
        raise HTTPException(status_code=404, detail="Display not found.")

    missing = [sid for sid in payload.screen_ids if scope.get(Screen, sid) is None]
    if missing:
        raise HTTPException(
            status_code=404, detail=f"No such screen: {', '.join(str(m) for m in missing)}."
        )

    display.rotation = (
        {"screen_ids": payload.screen_ids, "seconds": payload.seconds}
        if payload.screen_ids
        else {}
    )
    if payload.screen_ids:
        display.screen_id = payload.screen_ids[0]

    audit.record(
        scope, "display_token.rotation", actor_user_id=context.user.id,
        actor_label=context.user.email, target=display.name,
        detail=display.rotation,
    )
    scope.commit()
    return {"ok": True, "rotation": display.rotation}


@router.get("/preview")
def preview_live(
    mode: str = "whole_office",
    rank_by: str = "net_split",
    team: str = "",
    teams: list[str] = Query(default=[]),
    title: str = "",
    scope: TenantScope = Depends(user_scope),
) -> dict:
    """Render a combination that has not been saved as a screen.

    Used while someone is still choosing colours or a mode. Returns exactly
    what a television would receive, so the preview is the real thing rather
    than a drawing of it.
    """
    if mode not in MODES:
        raise HTTPException(status_code=404, detail=f"Unknown mode '{mode}'.")

    draft = Screen(
        org_id=scope.org_id,
        name="Preview",
        mode=mode,
        config={
            "rank_by": rank_by,
            "team": team,
            "teams": list(teams),
            "title": title or "PREVIEW",
            "refresh_seconds": 3600,
        },
    )
    return render(scope, draft)


@router.post("/display-tokens/{display_id}/regenerate")
def regenerate_display_token(
    display_id: int,
    context: AuthContext = Depends(require_role(Role.branch_manager)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    """Issue a new link for an existing television.

    The link is stored hashed and shown once, which is right until somebody
    closes the tab before writing it down — and then the display is
    unreachable with no way back. This is that way back. The previous link
    stops working immediately, which is also what you want if it leaked.
    """
    display = scope.get(DisplayToken, display_id)
    if display is None:
        raise HTTPException(status_code=404, detail="Display not found.")

    token, prefix, hashed = generate_token("tv")
    display.prefix = prefix
    display.token_hash = hashed
    display.revoked_at = None

    audit.record(
        scope, "display_token.regenerate", actor_user_id=context.user.id,
        actor_label=context.user.email, target=display.name,
    )
    scope.commit()
    return {
        "ok": True,
        "id": display.id,
        "name": display.name,
        "url": f"/tv/{token}",
        "note": "The previous link stopped working. Shown once.",
    }


@router.get("/teams/{team_id}/stats")
def team_stats(
    team_id: int,
    scope: TenantScope = Depends(user_scope),
) -> dict:
    """One team's figures, its people, and charts of both.

    The same roll-up and the same chart builder the television uses, so a team
    page in the console can never quietly disagree with the board on the wall.
    """
    from scoreboard.connectors.base import Period
    from scoreboard.domain import charts as chart_domain
    from scoreboard.domain.leaderboard import UNASSIGNED, rank
    from scoreboard.domain.metrics import METRIC_BY_KEY, roll_up
    from scoreboard.services.board import rows_for_period, trend

    team = scope.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found.")

    period = Period.current_month()
    everyone = rows_for_period(scope, period)
    members = [r for r in everyone if (r.get("team") or UNASSIGNED) == team.name]
    ranked = rank(members, "net_split")

    totals = roll_up(members)
    office = roll_up(everyone)

    # Share of the office, so a team's number means something next to the
    # others rather than only next to itself.
    share = {
        key: (totals[key] / office[key] * 100) if office.get(key) else 0.0
        for key in ("issued_leads", "sold_leads", "gross_split", "net_split")
    }

    widgets = []
    for spec in (
        {"type": "big_number", "metric": "net_split", "label": "Net split"},
        {"type": "big_number", "metric": "sold_leads", "label": "Sold leads"},
        {"type": "bar", "metric": "net_split", "group_by": "rep", "label": "Net by rep"},
        {"type": "bar", "metric": "close_rate", "group_by": "rep", "label": "Close rate by rep"},
        {"type": "donut", "metric": "sold_leads", "group_by": "rep", "label": "Share of sales"},
    ):
        try:
            widgets.append(chart_domain.build(spec, members))
        except chart_domain.ChartError:
            continue

    return {
        "team": {
            "id": team.id, "name": team.name,
            "lead_name": team.lead_name, "lead_role": team.lead_role,
        },
        "member_count": len(members),
        "totals": totals,
        "share_of_office": share,
        "reps": ranked,
        "widgets": widgets,
        "trend": trend(scope, period, "net_split"),
        "metric_labels": {k: v.label for k, v in METRIC_BY_KEY.items()},
        "metric_kinds": {k: v.kind for k, v in METRIC_BY_KEY.items()},
        "period": {"start": period.start.isoformat(), "end": period.end.isoformat()},
    }


@router.get("/overview")
def overview(scope: TenantScope = Depends(user_scope)) -> dict:
    """The whole office at a glance: totals, every team, and charts of both.

    Built from the same roll-up and the same chart builder the television
    uses. A dashboard that computed its own numbers would eventually disagree
    with the wall, and the wall is the one people trust.
    """
    from scoreboard.connectors.base import Period
    from scoreboard.domain import charts as chart_domain
    from scoreboard.domain.leaderboard import team_totals
    from scoreboard.domain.metrics import METRIC_BY_KEY, roll_up
    from scoreboard.services.board import rows_for_period, trend
    from scoreboard.services.screens import org_tokens, team_tokens

    period = Period.current_month()
    rows = rows_for_period(scope, period)
    totals = roll_up(rows)

    base = org_tokens(scope)
    overrides = team_tokens(scope)
    teams_by_name = {team.name: team for team in scope.all(Team)}

    standings = []
    for entry in team_totals(rows, "net_split"):
        team = teams_by_name.get(entry["team"])
        tokens = resolve(base, overrides.get(team.id)) if team else resolve(base)
        standings.append({
            "team_id": team.id if team else None,
            "team": entry["team"],
            "rank": entry["rank"],
            "rep_count": entry["rep_count"],
            "colour": tokens.get("primary"),
            "badge_url": tokens.get("badge_url", ""),
            "mvp": (entry.get("members") or [{}])[0].get("rep_name", ""),
            **{key: entry.get(key, 0) for key in METRIC_BY_KEY if key in entry},
        })

    widgets = []
    for spec in (
        {"type": "bar", "metric": "net_split", "group_by": "team", "label": "Net split by team"},
        {"type": "bar", "metric": "close_rate", "group_by": "team", "label": "Close rate by team"},
        {"type": "donut", "metric": "sold_leads", "group_by": "team", "label": "Share of sales"},
        {"type": "donut", "metric": "gross_split", "group_by": "team", "label": "Share of gross"},
        {"type": "leaders", "metric": "net_split", "group_by": "rep", "limit": 5,
         "label": "Top five people"},
    ):
        try:
            widgets.append(chart_domain.build(spec, rows))
        except chart_domain.ChartError:
            continue

    return {
        "period": {"start": period.start.isoformat(), "end": period.end.isoformat()},
        "rep_count": len(rows),
        "totals": totals,
        "standings": standings,
        "widgets": widgets,
        "trend": trend(scope, period, "net_split"),
        "metric_labels": {k: v.label for k, v in METRIC_BY_KEY.items()},
        "metric_kinds": {k: v.kind for k, v in METRIC_BY_KEY.items()},
    }
