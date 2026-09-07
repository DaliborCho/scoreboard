"""Where a number came from.

A leaderboard is arithmetic performed on somebody else's data and shown to a
room. The first thing anybody says about a figure they dislike is that it must
be wrong, and until there is an answer to "where did that come from", the whole
board is only as trusted as the person who installed it.

There are two layers to the answer, and this returns both:

1.  **The rows.** A group total is the sum of its people's components; an
    office total is the sum of everybody's. Nothing is stored to make this
    possible — it is simply the arithmetic run backwards and shown.
2.  **The row behind the row.** One person's figures came from a payload or an
    export, and `RepMetrics.source_row` kept it. So the trail ends at what the
    source actually said, months later, whether or not it still says it.

A derived metric shows its formula and the component totals it was evaluated
against, because "close rate is 16.2%" and "82 sales out of 506 leads" are the
same fact and only the second one settles an argument.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from scoreboard.domain.leaderboard import UNASSIGNED
from scoreboard.domain.metrics import DERIVED_ROLE, MetricCatalogue


@dataclass
class Trace:
    metric: str
    label: str
    kind: str
    role: str
    value: float
    #: The formula as the customer wrote it, when there is one.
    formula: str = ""
    #: The component totals the formula was evaluated against. Empty for a
    #: stored value, which is its own explanation.
    inputs: dict[str, float] = field(default_factory=dict)
    #: What to call each input on screen. A drill-down that says
    #: `issued_leads` is showing the reader our column names instead of theirs.
    input_labels: dict[str, str] = field(default_factory=dict)
    scope_label: str = ""
    rows: list[dict] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "label": self.label,
            "kind": self.kind,
            "role": self.role,
            "value": self.value,
            "formula": self.formula,
            "inputs": self.inputs,
            "input_labels": self.input_labels,
            "scope": self.scope_label,
            "rows": self.rows,
            "note": self.note,
        }


def explain(
    rows: list[dict],
    metric: str,
    metrics: MetricCatalogue,
    *,
    group: str = "",
    axis: str = "",
) -> Trace:
    """Take a number on a board apart, into the rows that made it.

    `rows` are board rows as `services.board` produced them, so this reruns
    the same roll-up the board did rather than a second implementation of it —
    an explanation that computed its own total could disagree with the figure
    it claims to explain, which would be worse than offering none.
    """
    definition = metrics.by_key.get(metric)
    if definition is None:
        raise KeyError(metric)

    def name_of(row: dict) -> str:
        # An axis names one grouping explicitly; without one, the grouping the
        # board is already using.
        found = (row.get("groups") or {}).get(axis) if axis else row.get("group")
        return found or UNASSIGNED

    considered = rows
    scope_label = "the whole office"
    if group:
        considered = [row for row in rows if name_of(row) == group]
        scope_label = group

    components = metrics.sum_components(considered)
    totals = metrics.derive(components)

    trace = Trace(
        metric=metric,
        label=definition.label,
        kind=definition.kind,
        role=definition.role,
        value=float(totals.get(metric) or 0),
        scope_label=scope_label,
    )

    if definition.role == DERIVED_ROLE and definition.formula:
        trace.formula = definition.formula.text
        # Only the names the formula actually uses, with the totals they had.
        # This is the line that turns "16.2%" into "82 out of 506".
        trace.inputs = {
            name: float(totals.get(name) or 0)
            for name in sorted(definition.formula.names)
            if name in totals
        }
        trace.input_labels = {
            name: (metrics.by_key[name].label if name in metrics.by_key else name)
            for name in trace.inputs
        }
        trace.note = (
            "Worked out from these totals, not averaged across the rows below. "
            "That is why it need not equal the average of the column."
        )
    elif definition.role != DERIVED_ROLE:
        trace.note = "Added up from the rows below."

    for row in sorted(
        considered, key=lambda r: float(r.get(metric) or 0), reverse=True
    ):
        trace.rows.append(
            {
                "rep_key": row.get("rep_key"),
                "rep_name": row.get("rep_name"),
                "group": row.get("group"),
                "value": float(row.get(metric) or 0),
                # The components this person contributed to the total. For a
                # derived metric these are what the formula names; for a stored
                # one it is the value itself.
                "inputs": {
                    name: float(row.get(name) or 0)
                    for name in (trace.inputs or {metric: 0})
                },
                "captured_on": row.get("captured_on"),
            }
        )
    return trace


def evidence(scope, rep_key: str, period) -> dict:
    """One person's captures for the period, and what the source sent.

    Every capture, not only the latest, because "it was different yesterday" is
    a claim the product should be able to settle rather than deny.
    """
    from sqlalchemy import select

    from scoreboard.models import Rep, RepMetrics

    rep = scope.one_by(Rep, rep_key=rep_key)
    if rep is None:
        return {}

    statement = (
        select(RepMetrics)
        .where(
            RepMetrics.org_id == scope.org_id,
            RepMetrics.rep_id == rep.id,
            RepMetrics.period_start == period.start,
            RepMetrics.period_end == period.end,
        )
        .order_by(RepMetrics.captured_on.desc())
    )

    captures = []
    for row in scope.session.scalars(statement).all():
        captures.append(
            {
                "captured_on": row.captured_on.isoformat(),
                "captured_at": row.captured_at.isoformat() if row.captured_at else "",
                "source": row.source_name,
                "values": dict(row.values or {}),
                "source_row": dict(row.source_row or {}),
            }
        )

    return {
        "rep": {
            "rep_key": rep.rep_key,
            "name": rep.name,
            "title": rep.title,
            "source_team": rep.source_team,
            "home_branch": rep.home_branch,
        },
        "captures": captures,
        "note": (
            "Every capture for this period, newest first. A board shows the "
            "newest; the rest are here because “it was different yesterday” "
            "should be answerable."
        ),
    }
