"""Chart widgets.

A deliberately short list. "Any chart you like" sounds generous and produces
boards nobody can read from the far side of a sales floor: scatter plots,
twelve-slice pies, dual axes. These six are the ones that survive distance,
and every one of them is computed on the server so the display only has to
draw what it is given.

Values are always derived through `metrics.roll_up`, never re-aggregated here,
so a chart cannot disagree with the table beside it.
"""
from __future__ import annotations

from dataclasses import dataclass

from scoreboard.domain.metrics import METRIC_BY_KEY, RANKABLE, roll_up

MAX_SERIES = 8  # Beyond this a board becomes a colour-matching puzzle.


@dataclass(frozen=True)
class ChartType:
    key: str
    label: str
    description: str
    needs_group: bool = False
    accepts_target: bool = False


CHART_TYPES: tuple[ChartType, ...] = (
    ChartType("big_number", "Big number", "One total, as large as the space allows."),
    ChartType("bar", "Bar", "Compare a metric across teams or reps.", needs_group=True),
    ChartType("donut", "Donut", "Share of a total. Additive metrics only.", needs_group=True),
    ChartType("trend", "Trend", "How a metric moved across the period, day by day."),
    ChartType("gauge", "Gauge", "Progress toward a target.", accepts_target=True),
    ChartType("leaders", "Leader list", "The top few, ranked.", needs_group=True),
)

CHART_BY_KEY = {c.key: c for c in CHART_TYPES}

# A share-of-total chart is only meaningful for values that add up. Averaging a
# close rate into a pie slice is a lie about what the slice represents.
from scoreboard.domain.metrics import ADDITIVE  # noqa: E402

SHARE_SAFE = set(ADDITIVE)


class ChartError(ValueError):
    """A widget configuration that cannot produce an honest chart."""


def validate_widget(widget: dict) -> list[str]:
    problems = []
    chart = CHART_BY_KEY.get(str(widget.get("type") or ""))
    if chart is None:
        return [f"Unknown chart type '{widget.get('type')}'. Known: {', '.join(CHART_BY_KEY)}."]

    metric = str(widget.get("metric") or "")
    if metric not in RANKABLE:
        problems.append(f"'{metric}' cannot be charted.")

    group_by = str(widget.get("group_by") or "team")
    if chart.needs_group and group_by not in ("team", "rep"):
        problems.append("Group by 'team' or 'rep'.")

    if chart.key == "donut" and metric not in SHARE_SAFE:
        problems.append(
            f"A donut shows share of a total, so '{metric}' cannot be used. "
            f"Choose one of: {', '.join(sorted(SHARE_SAFE))}."
        )

    if chart.accepts_target:
        try:
            float(widget.get("target") or 0)
        except (TypeError, ValueError):
            problems.append("Target must be a number.")

    return problems


def _grouped(rows: list[dict], group_by: str, metric: str) -> list[dict]:
    if group_by == "rep":
        return [
            {"label": row.get("rep_name", ""), "value": float(row.get(metric) or 0)}
            for row in rows
        ]

    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(row.get("team") or "Unassigned", []).append(row)
    # Totalled through the shared roll-up so a bar equals its table column.
    return [
        {"label": name, "value": float(roll_up(members).get(metric) or 0)}
        for name, members in buckets.items()
    ]


def build(widget: dict, rows: list[dict], trend_points: list[dict] | None = None) -> dict:
    """Compute one widget's data.

    Raises rather than returning something empty-but-plausible: a chart that
    silently shows zero is worse on a wall than a chart that is obviously
    misconfigured.
    """
    problems = validate_widget(widget)
    if problems:
        raise ChartError("; ".join(problems))

    chart = CHART_BY_KEY[widget["type"]]
    metric = widget["metric"]
    definition = METRIC_BY_KEY[metric]
    group_by = str(widget.get("group_by") or "team")
    limit = int(widget.get("limit") or MAX_SERIES)

    payload = {
        "type": chart.key,
        "metric": metric,
        "label": widget.get("label") or definition.label,
        "unit": definition.kind,
    }

    if chart.key == "big_number":
        payload["value"] = float(roll_up(rows).get(metric) or 0)
        return payload

    if chart.key == "trend":
        payload["points"] = trend_points or []
        return payload

    if chart.key == "gauge":
        payload["value"] = float(roll_up(rows).get(metric) or 0)
        payload["target"] = float(widget.get("target") or 0)
        return payload

    series = sorted(_grouped(rows, group_by, metric), key=lambda s: s["value"], reverse=True)
    trimmed = series[: min(limit, MAX_SERIES)]

    if chart.key == "donut":
        total = sum(s["value"] for s in series)
        remainder = total - sum(s["value"] for s in trimmed)
        if remainder > 0:
            # Named, not dropped. A donut whose slices do not sum to the whole
            # is the classic way a chart quietly misleads.
            trimmed = trimmed + [{"label": "Other", "value": remainder}]
        payload["total"] = total
    elif len(series) > len(trimmed):
        payload["hidden"] = len(series) - len(trimmed)

    payload["series"] = trimmed
    payload["group_by"] = group_by
    return payload


def catalogue() -> dict:
    return {
        "charts": [
            {
                "key": c.key,
                "label": c.label,
                "description": c.description,
                "needs_group": c.needs_group,
                "accepts_target": c.accepts_target,
            }
            for c in CHART_TYPES
        ],
        "metrics": [
            {"key": k, "label": METRIC_BY_KEY[k].label, "kind": METRIC_BY_KEY[k].kind}
            for k in RANKABLE
        ],
        "share_safe_metrics": sorted(SHARE_SAFE),
        "max_series": MAX_SERIES,
    }
