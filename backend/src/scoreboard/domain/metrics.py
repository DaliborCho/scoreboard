"""Metric catalogue and the one rule that governs every total.

There are two kinds of metric:

* **Additive** components come from the source and sum cleanly. Issued leads,
  sold leads and dollar amounts belong here.
* **Derived** metrics are ratios and averages. They are recomputed from the
  additive components of whatever group is being totalled.

Averaging a rate across people is the classic way to produce a leaderboard
that quietly lies: a rep with 1 issued lead and a 100% close rate would drag a
team average up as hard as a rep with 200. Derived values are therefore never
stored and never summed, only calculated at the moment a group is rolled up.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricDef:
    key: str
    label: str
    short_label: str
    kind: str  # number | currency | percent | text | system


# Raw components the source must supply. Everything else is calculated.
ADDITIVE = (
    "issued_leads",
    "pitched_leads",
    "sold_leads",
    "gross_split",
    "pending_split",
    "net_split",
)

METRIC_DEFS: tuple[MetricDef, ...] = (
    MetricDef("rank", "Rank", "#", "system"),
    MetricDef("rep_name", "Sales Rep", "REP", "text"),
    MetricDef("team", "Team", "TEAM", "text"),
    MetricDef("home_branch", "Home Branch", "BRANCH", "text"),
    MetricDef("title", "Title", "TITLE", "text"),
    MetricDef("hire_date", "Hire Date", "HIRED", "text"),
    MetricDef("issued_leads", "Issued Leads", "ISS", "number"),
    MetricDef("pitched_leads", "Pitched Leads", "PIT", "number"),
    MetricDef("pitched_rate", "Pitched Rate", "PIT%", "percent"),
    MetricDef("sold_leads", "Sold Leads", "SOLD", "number"),
    MetricDef("close_rate", "Close Rate", "CLS%", "percent"),
    MetricDef("gross_split", "Gross Split", "GROSS", "currency"),
    MetricDef("pending_split", "Pending Split", "PEND", "currency"),
    MetricDef("net_split", "Net Split", "NET", "currency"),
    MetricDef("dpl", "DPL", "DPL", "currency"),
    MetricDef("sales_retention", "Sales Retention", "RET%", "percent"),
    MetricDef("avg_gross_sale", "Avg. Gross Sale", "AGS", "currency"),
    MetricDef("avg_net_sale", "Avg. Net Sale", "ANS", "currency"),
)

METRIC_BY_KEY = {m.key: m for m in METRIC_DEFS}

# Metrics that can be ranked on.
RANKABLE = tuple(m.key for m in METRIC_DEFS if m.kind in {"number", "currency", "percent"})


def _ratio(numerator: float, denominator: float, scale: float = 1.0) -> float:
    """Division that yields 0 rather than raising when the denominator is empty."""
    if not denominator:
        return 0.0
    return (numerator / denominator) * scale


def derive(components: dict[str, float]) -> dict[str, float]:
    """Expand additive components into the full metric set.

    Accepts the raw components of a rep, a team or a whole office without
    caring which, because the arithmetic is identical at every level. That is
    the point: one implementation, so a team total can never disagree with the
    sum of its rows.
    """
    issued = float(components.get("issued_leads") or 0)
    pitched = float(components.get("pitched_leads") or 0)
    sold = float(components.get("sold_leads") or 0)
    gross = float(components.get("gross_split") or 0)
    pending = float(components.get("pending_split") or 0)
    net = float(components.get("net_split") or 0)

    return {
        "issued_leads": issued,
        "pitched_leads": pitched,
        "sold_leads": sold,
        "gross_split": gross,
        "pending_split": pending,
        "net_split": net,
        "pitched_rate": _ratio(pitched, issued, 100.0),
        "close_rate": _ratio(sold, issued, 100.0),
        "dpl": _ratio(net, issued),
        "sales_retention": _ratio(net, gross, 100.0),
        "avg_gross_sale": _ratio(gross, sold),
        "avg_net_sale": _ratio(net, sold),
    }


def sum_components(rows: list[dict]) -> dict[str, float]:
    """Add up only the additive components across a group of rows."""
    total = dict.fromkeys(ADDITIVE, 0.0)
    for row in rows:
        for key in ADDITIVE:
            total[key] += float(row.get(key) or 0)
    return total


def roll_up(rows: list[dict]) -> dict[str, float]:
    """Total a group correctly: sum the components, then derive the rest."""
    return derive(sum_components(rows))
