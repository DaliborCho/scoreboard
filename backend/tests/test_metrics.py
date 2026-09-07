from scoreboard.domain.metrics import derive, roll_up


def test_derived_values_come_from_components():
    result = derive(
        {"issued_leads": 40, "pitched_leads": 30, "sold_leads": 10,
         "gross_split": 100_000, "pending_split": 10_000, "net_split": 80_000}
    )
    assert result["pitched_rate"] == 75.0
    assert result["close_rate"] == 25.0
    assert result["dpl"] == 2000.0
    assert result["sales_retention"] == 80.0
    assert result["avg_gross_sale"] == 10_000.0
    assert result["avg_net_sale"] == 8_000.0


def test_empty_denominator_is_zero_not_an_error():
    result = derive({"issued_leads": 0, "sold_leads": 0, "gross_split": 0, "net_split": 0})
    assert result["close_rate"] == 0.0
    assert result["avg_gross_sale"] == 0.0


def test_group_rate_is_recomputed_not_averaged():
    """The rule the whole metric module exists to enforce.

    A rep with one lead and a perfect close rate must not pull a team average
    up as hard as a rep carrying two hundred leads.
    """
    heavy = {"issued_leads": 200, "sold_leads": 40, "gross_split": 0, "net_split": 0,
             "pitched_leads": 0, "pending_split": 0}
    light = {"issued_leads": 1, "sold_leads": 1, "gross_split": 0, "net_split": 0,
             "pitched_leads": 0, "pending_split": 0}

    total = roll_up([heavy, light])

    naive_average = (derive(heavy)["close_rate"] + derive(light)["close_rate"]) / 2
    assert round(total["close_rate"], 4) == round(41 / 201 * 100, 4)
    assert total["close_rate"] < naive_average


def test_roll_up_sums_only_additive_components():
    total = roll_up([
        {"issued_leads": 10, "sold_leads": 2, "gross_split": 1000, "net_split": 900,
         "pitched_leads": 5, "pending_split": 100},
        {"issued_leads": 30, "sold_leads": 6, "gross_split": 3000, "net_split": 2700,
         "pitched_leads": 15, "pending_split": 300},
    ])
    assert total["issued_leads"] == 40
    assert total["gross_split"] == 4000
    assert total["close_rate"] == 20.0


# ---------------------------------------------------------------- a catalogue is data
def custom_catalogue():
    """A company that measures something this product never shipped.

    Nothing here appears anywhere in the codebase. If these tests pass, the
    metric set is genuinely data rather than a list we happened to choose.
    """
    from scoreboard.domain.metrics import MetricCatalogue, MetricDef, ratio

    return MetricCatalogue([
        MetricDef("rep_name", "Technician", "TECH", "text"),
        MetricDef("jobs_booked", "Jobs Booked", "BOOK", "number", role="additive"),
        MetricDef("jobs_done", "Jobs Completed", "DONE", "number", role="additive"),
        MetricDef("callbacks", "Callbacks", "CB", "number", role="additive"),
        MetricDef("revenue", "Revenue", "REV", "currency", role="additive"),
        MetricDef("completion_rate", "Completion Rate", "CMP%", "percent",
                  role="derived", formula=ratio("jobs_done", "jobs_booked", 100.0)),
        MetricDef("callback_rate", "Callback Rate", "CB%", "percent",
                  role="derived", formula=ratio("callbacks", "jobs_done", 100.0)),
        MetricDef("revenue_per_job", "Revenue per Job", "RPJ", "currency",
                  role="derived", formula=ratio("revenue", "jobs_done")),
    ])


def test_a_catalogue_nobody_shipped_still_works():
    catalogue = custom_catalogue()
    assert catalogue.additive == ("jobs_booked", "jobs_done", "callbacks", "revenue")

    values = catalogue.derive({"jobs_booked": 50, "jobs_done": 40, "callbacks": 4,
                               "revenue": 120_000})
    assert values["completion_rate"] == 80.0
    assert values["callback_rate"] == 10.0
    assert values["revenue_per_job"] == 3_000.0


def test_the_summing_rule_holds_for_metrics_we_never_chose():
    """The rule is a property of the arithmetic, not of our six components."""
    catalogue = custom_catalogue()
    heavy = {"jobs_booked": 200, "jobs_done": 40, "callbacks": 0, "revenue": 0}
    light = {"jobs_booked": 1, "jobs_done": 1, "callbacks": 0, "revenue": 0}

    total = catalogue.roll_up([heavy, light])
    naive = (catalogue.derive(heavy)["completion_rate"]
             + catalogue.derive(light)["completion_rate"]) / 2

    assert round(total["completion_rate"], 4) == round(41 / 201 * 100, 4)
    assert total["completion_rate"] < naive


def test_a_derived_metric_may_build_on_an_earlier_one():
    """Declaration order is evaluation order, so ratios can compose."""
    from scoreboard.domain.metrics import MetricCatalogue, MetricDef, ratio

    catalogue = MetricCatalogue([
        MetricDef("a", "A", "A", "number", role="additive"),
        MetricDef("b", "B", "B", "number", role="additive"),
        MetricDef("ratio", "Ratio", "R", "number", role="derived", formula=ratio("a", "b")),
        MetricDef("half", "Half", "H", "number", role="derived", formula=ratio("ratio", "b")),
    ])
    values = catalogue.derive({"a": 100, "b": 4})
    assert values["ratio"] == 25.0
    assert values["half"] == 6.25


def test_the_shipped_catalogue_is_one_of_these():
    """The default is not a special case; it is an instance."""
    from scoreboard.domain.metrics import DEFAULT_CATALOGUE, MetricCatalogue

    assert isinstance(DEFAULT_CATALOGUE, MetricCatalogue)
    assert len(DEFAULT_CATALOGUE.additive) == 6
    assert len(DEFAULT_CATALOGUE.derived) == 6
    assert set(DEFAULT_CATALOGUE.by_key) >= {"close_rate", "dpl", "net_split"}


def test_every_derived_metric_names_operands_that_exist():
    """A formula pointing at a metric nobody defined would silently read zero."""
    from scoreboard.domain.metrics import DEFAULT_CATALOGUE as c

    for metric in c.derived:
        for name in metric.formula.names:
            assert name in c.by_key, f"{metric.key} uses {name}"


def test_every_shipped_formula_only_uses_what_comes_before_it():
    """Evaluation is in declaration order, so a later name would read zero."""
    from scoreboard.domain.metrics import DEFAULT_CATALOGUE as c

    seen: set[str] = set()
    for metric in c.definitions:
        if metric.formula:
            for name in sorted(metric.formula.names):
                assert name in seen, f"{metric.key} uses {name} before it exists"
        seen.add(metric.key)
