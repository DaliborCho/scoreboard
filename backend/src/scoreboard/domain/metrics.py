"""The metric catalogue, and the one rule that governs every total.

There are two kinds of metric:

* **Additive** components come from the source and sum cleanly. Issued leads,
  sold leads and dollar amounts belong here.
* **Derived** metrics are ratios and averages. They carry their own formula and
  are recomputed from the additive components of whatever group is being
  totalled.

Averaging a rate across people is the classic way to produce a leaderboard
that quietly lies: a rep with 1 issued lead and a 100% close rate would drag a
team average up as hard as a rep with 200. Derived values are therefore never
stored and never summed, only calculated at the moment a group is rolled up.

A catalogue is a value, not a module constant. `DEFAULT_CATALOGUE` is the set
this product shipped with; a customer's own catalogue is the same shape loaded
from their rows. Every caller that used to reach for the module constants can
take a catalogue instead, one at a time, without anything changing while it
still passes the default.
"""
from __future__ import annotations

from dataclasses import dataclass

from scoreboard.domain.formula import Expression, FormulaError

ADDITIVE_ROLE = "additive"
DERIVED_ROLE = "derived"
TEXT_ROLE = "text"
SYSTEM_ROLE = "system"

NUMERIC_KINDS = frozenset({"number", "currency", "percent"})


def ratio(numerator: str, denominator: str, scale: float = 1.0) -> Expression:
    """The common case, written as the expression it is.

    Most derived metrics really are one division: `close_rate` is
    `sold_leads / issued_leads * 100`. The console still offers that as two
    fields and a multiplier, because it is what most people want and a text box
    is a worse way to say it. This is where that shorthand becomes the one
    representation everything else evaluates.
    """
    text = f"{numerator} / {denominator}"
    if scale != 1.0:
        text = f"({text}) * {scale:g}"
    return Expression.parse(text, shorthand=(numerator, denominator, float(scale)))


@dataclass(frozen=True)
class MetricDef:
    key: str
    label: str
    short_label: str
    kind: str  # number | currency | percent | text | system
    role: str = TEXT_ROLE
    formula: Expression | None = None

    @property
    def is_additive(self) -> bool:
        return self.role == ADDITIVE_ROLE

    @property
    def is_derived(self) -> bool:
        return self.role == DERIVED_ROLE


def _additive(key: str, label: str, short: str, kind: str) -> MetricDef:
    return MetricDef(key, label, short, kind, role=ADDITIVE_ROLE)


def _derived(key: str, label: str, short: str, kind: str, formula: Expression) -> MetricDef:
    return MetricDef(key, label, short, kind, role=DERIVED_ROLE, formula=formula)


class MetricCatalogue:
    """An ordered set of metric definitions, and the arithmetic over them."""

    def __init__(self, definitions: tuple[MetricDef, ...] | list[MetricDef]):
        self.definitions: tuple[MetricDef, ...] = tuple(definitions)
        self.by_key: dict[str, MetricDef] = {m.key: m for m in self.definitions}

        self.additive: tuple[str, ...] = tuple(
            m.key for m in self.definitions if m.is_additive
        )
        # Declaration order is evaluation order, so a derived metric may be
        # built from one declared before it.
        self.derived: tuple[MetricDef, ...] = tuple(
            m for m in self.definitions if m.is_derived and m.formula
        )
        self.rankable: tuple[str, ...] = tuple(
            m.key for m in self.definitions if m.kind in NUMERIC_KINDS
        )

    # ------------------------------------------------------------ arithmetic
    def derive(self, components: dict[str, float]) -> dict[str, float]:
        """Expand additive components into the full metric set.

        Accepts the raw components of a rep, a team or a whole office without
        caring which, because the arithmetic is identical at every level. That
        is the point: one implementation, so a team total can never disagree
        with the sum of its rows.
        """
        values: dict[str, float] = {
            key: float(components.get(key) or 0) for key in self.additive
        }
        for metric in self.derived:
            # Evaluated in declaration order against everything computed so
            # far, so one derived metric may be built from another — and
            # always from summed components, never from other people's rates.
            values[metric.key] = metric.formula.evaluate(values)
        return values

    def sum_components(self, rows: list[dict]) -> dict[str, float]:
        """Add up only the additive components across a group of rows."""
        total = dict.fromkeys(self.additive, 0.0)
        for row in rows:
            for key in self.additive:
                total[key] += float(row.get(key) or 0)
        return total

    def roll_up(self, rows: list[dict]) -> dict[str, float]:
        """Total a group correctly: sum the components, then derive the rest."""
        return self.derive(self.sum_components(rows))


DEFAULT_DEFINITIONS: tuple[MetricDef, ...] = (
    MetricDef("rank", "Rank", "#", "system", role=SYSTEM_ROLE),
    MetricDef("rep_name", "Sales Rep", "REP", "text"),
    # Labelled at render time with whatever the board is grouped by --
    # "Team", "Branch", "Region" -- because that is a choice a customer
    # makes now rather than a word this file gets to fix.
    MetricDef("group", "Group", "GROUP", "text"),
    MetricDef("home_branch", "Home Branch", "BRANCH", "text"),
    MetricDef("title", "Title", "TITLE", "text"),
    MetricDef("hire_date", "Hire Date", "HIRED", "text"),

    _additive("issued_leads", "Issued Leads", "ISS", "number"),
    _additive("pitched_leads", "Pitched Leads", "PIT", "number"),
    _derived("pitched_rate", "Pitched Rate", "PIT%", "percent",
             ratio("pitched_leads", "issued_leads", 100.0)),
    _additive("sold_leads", "Sold Leads", "SOLD", "number"),
    _derived("close_rate", "Close Rate", "CLS%", "percent",
             ratio("sold_leads", "issued_leads", 100.0)),
    _additive("gross_split", "Gross Split", "GROSS", "currency"),
    _additive("pending_split", "Pending Split", "PEND", "currency"),
    _additive("net_split", "Net Split", "NET", "currency"),
    _derived("dpl", "DPL", "DPL", "currency",
             ratio("net_split", "issued_leads")),
    _derived("sales_retention", "Sales Retention", "RET%", "percent",
             ratio("net_split", "gross_split", 100.0)),
    _derived("avg_gross_sale", "Avg. Gross Sale", "AGS", "currency",
             ratio("gross_split", "sold_leads")),
    _derived("avg_net_sale", "Avg. Net Sale", "ANS", "currency",
             ratio("net_split", "sold_leads")),
)

DEFAULT_CATALOGUE = MetricCatalogue(DEFAULT_DEFINITIONS)


# ---------------------------------------------------------------- compatibility
# The shipped catalogue, reachable the way it always was. Call sites move to an
# explicit catalogue one at a time; until then they get this one and behave
# exactly as before.
METRIC_DEFS = DEFAULT_CATALOGUE.definitions
METRIC_BY_KEY = DEFAULT_CATALOGUE.by_key
ADDITIVE = DEFAULT_CATALOGUE.additive
RANKABLE = DEFAULT_CATALOGUE.rankable

__all__ = [
    "ADDITIVE",
    "ADDITIVE_ROLE",
    "DEFAULT_CATALOGUE",
    "DEFAULT_DEFINITIONS",
    "DERIVED_ROLE",
    "METRIC_BY_KEY",
    "METRIC_DEFS",
    "NUMERIC_KINDS",
    "RANKABLE",
    "SYSTEM_ROLE",
    "TEXT_ROLE",
    "Expression",
    "FormulaError",
    "MetricCatalogue",
    "MetricDef",
    "derive",
    "ratio",
    "roll_up",
    "sum_components",
]

derive = DEFAULT_CATALOGUE.derive
sum_components = DEFAULT_CATALOGUE.sum_components
roll_up = DEFAULT_CATALOGUE.roll_up
