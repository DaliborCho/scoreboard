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
