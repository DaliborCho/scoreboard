"""Leaderboard assembly: ranking, group roll-ups and the display modes.

Pure functions over plain dictionaries. Nothing here touches the database, the
web framework or any source system, so the rules can be tested directly and
reused by every screen.

Rows carry `group`, not `team`. Which axis that is — team, branch, region, or
something a customer invented — is decided when the rows are read, so the same
four modes serve every grouping instead of the one the code happened to know
about.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from scoreboard.domain.metrics import DEFAULT_CATALOGUE, MetricCatalogue

UNASSIGNED = "Unassigned"


@dataclass
class RepRow:
    rep_key: str
    rep_name: str
    #: The grouping in effect for this board.
    group: str = UNASSIGNED
    #: Every axis this person sits on, keyed by group type. Carried so a chart
    #: can break down by branch while the board beside it is ranked by team,
    #: off one set of rows rather than a second query.
    groups: dict[str, str] = field(default_factory=dict)
    home_branch: str = ""
    title: str = ""
    hire_date: str = ""
    components: dict[str, float] = field(default_factory=dict)

    def as_row(self, metrics: MetricCatalogue = DEFAULT_CATALOGUE) -> dict:
        row = {
            "rep_key": self.rep_key,
            "rep_name": self.rep_name,
            "group": self.group,
            "groups": dict(self.groups),
            "home_branch": self.home_branch,
            "title": self.title,
            "hire_date": self.hire_date,
        }
        row.update(metrics.derive(self.components))
        return row


def rank(rows: list[dict], metric: str,
         metrics: MetricCatalogue = DEFAULT_CATALOGUE) -> list[dict]:
    """Sort high-to-low on one metric and stamp a 1-based rank.

    There is exactly one ranking rule in the product: highest wins. The
    original had a per-mode direction toggle and it produced more support
    questions than value, so the direction is not configurable.
    """
    if metric not in metrics.rankable:
        # Fall back to the first rankable metric this organization has, which
        # for the shipped catalogue is still net split.
        metric = "net_split" if "net_split" in metrics.rankable else (
            metrics.rankable[0] if metrics.rankable else metric)
    ordered = sorted(
        rows,
        # Name breaks a tie, so an unchanged board does not reshuffle between
        # refreshes and make people think a number moved.
        key=lambda r: (float(r.get(metric) or 0), r.get("rep_name", "")),
        reverse=True,
    )
    for position, row in enumerate(ordered, start=1):
        row["rank"] = position
    return ordered


def split_by_group(rows: list[dict], axis: str = "") -> dict[str, list[dict]]:
    """Bucket rows by the grouping in effect, or by a named axis instead.

    Passing an axis is what lets a board ranked by team carry a chart broken
    down by branch, from the same rows.
    """
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        if axis:
            name = (row.get("groups") or {}).get(axis) or UNASSIGNED
        else:
            name = row.get("group") or UNASSIGNED
        buckets.setdefault(name, []).append(row)
    return buckets


def group_totals(rows: list[dict], metric: str,
                 metrics: MetricCatalogue = DEFAULT_CATALOGUE,
                 axis: str = "") -> list[dict]:
    """One totalled entry per group, ranked by the same metric as the reps."""
    totals = []
    for name, members in split_by_group(rows, axis).items():
        entry = {"group": name, "rep_count": len(members)}
        entry.update(metrics.roll_up(members))
        entry["members"] = rank(members, metric, metrics)
        totals.append(entry)

    ordered = sorted(totals, key=lambda t: (float(t.get(metric) or 0), t["group"]), reverse=True)
    for position, entry in enumerate(ordered, start=1):
        entry["rank"] = position
    return ordered


# ---------------------------------------------------------------- display modes
def whole_office(rows: list[dict], metric: str = "net_split",
                 metrics: MetricCatalogue = DEFAULT_CATALOGUE) -> dict:
    ranked = rank(list(rows), metric, metrics)
    return {
        "mode": "whole_office",
        "rank_by": metric,
        "reps": ranked,
        "total": metrics.roll_up(ranked),
    }


def per_group(rows: list[dict], group: str, metric: str = "net_split",
              metrics: MetricCatalogue = DEFAULT_CATALOGUE) -> dict:
    members = [r for r in rows if (r.get("group") or UNASSIGNED) == group]
    ranked = rank(members, metric, metrics)
    return {
        "mode": "per_group",
        "rank_by": metric,
        "group": group,
        "reps": ranked,
        "total": metrics.roll_up(ranked),
    }


def group_vs_group(rows: list[dict], names: list[str], metric: str = "net_split",
                   metrics: MetricCatalogue = DEFAULT_CATALOGUE) -> dict:
    """Head-to-head between exactly two groups.

    The limit is deliberate and enforced here rather than in the UI: the
    layout is a two-column scoreboard, and a third group has nowhere to go.
    """
    selected = [name for name in names if name][:2]
    totals = [t for t in group_totals(rows, metric, metrics) if t["group"] in selected]
    ordered = sorted(totals, key=lambda t: selected.index(t["group"]))
    ordered = sorted(ordered, key=lambda t: float(t.get(metric) or 0), reverse=True)
    for position, entry in enumerate(ordered, start=1):
        entry["placement"] = "WINNER" if position == 1 else ""
    return {"mode": "group_vs_group", "rank_by": metric, "groups": ordered}


def all_groups(rows: list[dict], metric: str = "net_split",
               metrics: MetricCatalogue = DEFAULT_CATALOGUE) -> dict:
    """Group cards with an MVP each: the top rep inside by the same metric."""
    totals = group_totals(rows, metric, metrics)
    for entry in totals:
        members = entry.get("members") or []
        entry["mvp"] = members[0] if members else None
    return {"mode": "all_groups", "rank_by": metric, "groups": totals}


MODES = {
    "whole_office": whole_office,
    "per_group": per_group,
    "group_vs_group": group_vs_group,
    "all_groups": all_groups,
}

#: Screens saved while `team` was the only axis the product had. Kept so a
#: customer's existing walls keep drawing after the upgrade rather than
#: raising "unknown mode" at a television nobody is standing next to.
LEGACY_MODES = {
    "per_team": "per_group",
    "team_vs_team": "group_vs_group",
    "all_teams": "all_groups",
}


def canonical_mode(mode: str) -> str:
    return LEGACY_MODES.get(mode, mode or "whole_office")
