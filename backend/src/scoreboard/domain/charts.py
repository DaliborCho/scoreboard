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

from scoreboard.domain.metrics import DEFAULT_CATALOGUE, MetricCatalogue

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
    ChartType("bar", "Bar", "Compare a metric across groups or reps.", needs_group=True),
    ChartType("donut", "Donut", "Share of a total. Additive metrics only.", needs_group=True),
    ChartType("trend", "Trend", "How a metric moved across the period, day by day."),
    ChartType("gauge", "Gauge", "Progress toward a target.", accepts_target=True),
    ChartType("leaders", "Leader list", "The top few, ranked.", needs_group=True),
)

CHART_BY_KEY = {c.key: c for c in CHART_TYPES}

# A share-of-total chart is only meaningful for values that add up. Averaging a
# close rate into a pie slice is a lie about what the slice represents. Which
# values those are now depends on the organization's own catalogue.
def share_safe(metrics: MetricCatalogue = DEFAULT_CATALOGUE) -> set[str]:
    return set(metrics.additive)


class ChartError(ValueError):
    """A widget configuration that cannot produce an honest chart."""


def validate_widget(widget: dict,
                    metrics: MetricCatalogue = DEFAULT_CATALOGUE,
                    axes: tuple[str, ...] = ()) -> list[str]:
    """Everything wrong with a widget definition, in the customer's words.

    `axes` are the group types this organization actually has. Passing them is
    what lets "group by region" be accepted here without this module knowing
    that regions exist.
    """
    problems = []
    chart = CHART_BY_KEY.get(str(widget.get("type") or ""))
    if chart is None:
        return [f"Unknown chart type '{widget.get('type')}'. Known: {', '.join(CHART_BY_KEY)}."]

    metric = str(widget.get("metric") or "")
    if metric not in metrics.rankable:
        problems.append(f"'{metric}' cannot be charted.")

    # "group" means whichever axis the board itself is grouped by. A named
    # axis -- branch, region -- breaks the same rows down a different way, so a
    # board ranked by team can carry a chart of branches beside it.
    group_by = str(widget.get("group_by") or "group")
    allowed = ("group", "rep", *axes)
    if chart.needs_group and group_by not in allowed:
        problems.append(f"Group by one of: {', '.join(allowed)}.")

    if chart.key == "donut" and metric not in share_safe(metrics):
        problems.append(
            f"A donut shows share of a total, so '{metric}' cannot be used. "
            f"Choose one of: {', '.join(sorted(share_safe(metrics)))}."
        )

    if chart.accepts_target:
        try:
            float(widget.get("target") or 0)
        except (TypeError, ValueError):
            problems.append("Target must be a number.")

    return problems


def _grouped(rows: list[dict], group_by: str, metric: str,
             metrics: MetricCatalogue = DEFAULT_CATALOGUE) -> list[dict]:
    if group_by == "rep":
        return [
            {"label": row.get("rep_name", ""), "value": float(row.get(metric) or 0)}
            for row in rows
        ]

    buckets: dict[str, list[dict]] = {}
    for row in rows:
        if group_by == "group":
            name = row.get("group")
        else:
            name = (row.get("groups") or {}).get(group_by)
        buckets.setdefault(name or "Unassigned", []).append(row)
    # Totalled through the shared roll-up so a bar equals its table column.
    return [
        {
            "label": name,
            "value": float(metrics.roll_up(members).get(metric) or 0),
        }
        for name, members in buckets.items()
    ]


def build(widget: dict, rows: list[dict], trend_points: list[dict] | None = None,
          metrics: MetricCatalogue = DEFAULT_CATALOGUE) -> dict:
    """Compute one widget's data.

    Raises rather than returning something empty-but-plausible: a chart that
    silently shows zero is worse on a wall than a chart that is obviously
    misconfigured.
    """
    # The axes are read off the rows rather than passed in, because the rows
    # are the only honest answer to "what can this be grouped by": a chart
    # cannot break figures down along an axis the figures do not carry.
    axes = tuple(sorted({key for row in rows for key in (row.get("groups") or {})}))
    if not rows:
        # Nothing to compute and nothing to contradict. Refusing here would put
        # a configuration error on a wall whose only real problem is an empty
        # month.
        axes = (str(widget.get("group_by") or ""),)

    problems = validate_widget(widget, metrics, axes)
    if problems:
        raise ChartError("; ".join(problems))

    chart = CHART_BY_KEY[widget["type"]]
    metric = widget["metric"]
    definition = metrics.by_key[metric]
    group_by = str(widget.get("group_by") or "group")
    limit = int(widget.get("limit") or MAX_SERIES)

    payload = {
        "type": chart.key,
        "metric": metric,
        "label": widget.get("label") or definition.label,
        "unit": definition.kind,
    }

    if chart.key == "big_number":
        payload["value"] = float(metrics.roll_up(rows).get(metric) or 0)
        return payload

    if chart.key == "trend":
        payload["points"] = trend_points or []
        return payload

    if chart.key == "gauge":
        payload["value"] = float(metrics.roll_up(rows).get(metric) or 0)
        payload["target"] = float(widget.get("target") or 0)
        return payload

    series = sorted(
        _grouped(rows, group_by, metric, metrics),
        key=lambda entry: entry["value"],
        reverse=True,
    )
    trimmed = series[: min(limit, MAX_SERIES)]

    if chart.key == "donut":
        total = sum(s["value"] for s in series)
        remainder = total - sum(s["value"] for s in trimmed)
        if remainder > 0:
            # Named, not dropped. A donut whose slices do not sum to the whole
            # is the classic way a chart quietly misleads.
            trimmed = [*trimmed, {"label": "Other", "value": remainder}]
        payload["total"] = total
    elif len(series) > len(trimmed):
        payload["hidden"] = len(series) - len(trimmed)

    payload["series"] = trimmed
    payload["group_by"] = group_by
    return payload


def catalogue(metrics: MetricCatalogue = DEFAULT_CATALOGUE) -> dict:
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
            {"key": k, "label": metrics.by_key[k].label, "kind": metrics.by_key[k].kind}
            for k in metrics.rankable
        ],
        "share_safe_metrics": sorted(share_safe(metrics)),
        "max_series": MAX_SERIES,
    }
