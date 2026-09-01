"""Leaderboard assembly: ranking, team roll-ups and the display modes.

Pure functions over plain dictionaries. Nothing here touches the database, the
web framework or any source system, so the rules can be tested directly and
reused by every screen.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from scoreboard.domain.metrics import RANKABLE, derive, roll_up

UNASSIGNED = "Unassigned"


@dataclass
class RepRow:
    rep_key: str
    rep_name: str
    team: str = UNASSIGNED
    home_branch: str = ""
    title: str = ""
    hire_date: str = ""
    components: dict[str, float] = field(default_factory=dict)

    def as_row(self) -> dict:
        row = {
            "rep_key": self.rep_key,
            "rep_name": self.rep_name,
            "team": self.team,
            "home_branch": self.home_branch,
            "title": self.title,
            "hire_date": self.hire_date,
        }
        row.update(derive(self.components))
        return row


def rank(rows: list[dict], metric: str) -> list[dict]:
    """Sort high-to-low on one metric and stamp a 1-based rank.

    There is exactly one ranking rule in the product: highest wins. The
    original had a per-mode direction toggle and it produced more support
    questions than value, so the direction is not configurable.
    """
    if metric not in RANKABLE:
        metric = "net_split"
    ordered = sorted(rows, key=lambda r: (float(r.get(metric) or 0), r.get("rep_name", "")), reverse=True)
    for position, row in enumerate(ordered, start=1):
        row["rank"] = position
    return ordered


def group_by_team(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row.get("team") or UNASSIGNED, []).append(row)
    return groups


def team_totals(rows: list[dict], metric: str) -> list[dict]:
    """One totalled entry per team, ranked by the same metric as the reps."""
    totals = []
    for team_name, members in group_by_team(rows).items():
        entry = {"team": team_name, "rep_count": len(members)}
        entry.update(roll_up(members))
        entry["members"] = rank(members, metric)
        totals.append(entry)

    ordered = sorted(totals, key=lambda t: (float(t.get(metric) or 0), t["team"]), reverse=True)
    for position, entry in enumerate(ordered, start=1):
        entry["rank"] = position
    return ordered


# ---------------------------------------------------------------- display modes
def whole_office(rows: list[dict], metric: str = "net_split") -> dict:
    ranked = rank(list(rows), metric)
    return {
        "mode": "whole_office",
        "rank_by": metric,
        "reps": ranked,
        "total": roll_up(ranked),
    }


def per_team(rows: list[dict], team: str, metric: str = "net_split") -> dict:
    members = [r for r in rows if (r.get("team") or UNASSIGNED) == team]
    ranked = rank(members, metric)
    return {
        "mode": "per_team",
        "rank_by": metric,
        "team": team,
        "reps": ranked,
        "total": roll_up(ranked),
    }


def team_vs_team(rows: list[dict], teams: list[str], metric: str = "net_split") -> dict:
    """Head-to-head between exactly two teams.

    The limit is deliberate and enforced here rather than in the UI: the
    layout is a two-column scoreboard, and a third team has nowhere to go.
    """
    selected = [t for t in teams if t][:2]
    totals = [t for t in team_totals(rows, metric) if t["team"] in selected]
    ordered = sorted(totals, key=lambda t: selected.index(t["team"]))
    ordered = sorted(ordered, key=lambda t: float(t.get(metric) or 0), reverse=True)
    for position, entry in enumerate(ordered, start=1):
        entry["placement"] = "WINNER" if position == 1 else ""
    return {"mode": "team_vs_team", "rank_by": metric, "teams": ordered}


def all_teams(rows: list[dict], metric: str = "net_split") -> dict:
    """Team cards with an MVP each: the top rep on that team by the same metric."""
    totals = team_totals(rows, metric)
    for entry in totals:
        members = entry.get("members") or []
        entry["mvp"] = members[0] if members else None
    return {"mode": "all_teams", "rank_by": metric, "teams": totals}


MODES = {
    "whole_office": whole_office,
    "per_team": per_team,
    "team_vs_team": team_vs_team,
    "all_teams": all_teams,
}
