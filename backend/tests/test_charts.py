"""Chart rules.

Charts are the easiest place in a product like this to publish something
untrue on a wall, so most of these tests are about what the system refuses to
draw rather than what it draws.
"""
import pytest

from scoreboard.domain import charts
from scoreboard.domain.leaderboard import RepRow


def rows():
    def rep(name, team, issued, sold, gross, net):
        return RepRow(
            rep_key=name.lower(), rep_name=name, team=team,
            components={
                "issued_leads": issued, "pitched_leads": issued * 0.6, "sold_leads": sold,
                "gross_split": gross, "pending_split": 0, "net_split": net,
            },
        ).as_row()

    return [
        rep("A", "Alpha", 100, 25, 250_000, 200_000),
        rep("B", "Alpha", 50, 5, 50_000, 40_000),
        rep("C", "Bravo", 40, 20, 300_000, 260_000),
        rep("D", "Charlie", 10, 1, 9_000, 8_000),
    ]


# ---------------------------------------------------------------- validation
def test_unknown_chart_type_is_rejected():
    assert charts.validate_widget({"type": "sankey", "metric": "net_split"})


def test_unchartable_metric_is_rejected():
    assert charts.validate_widget({"type": "bar", "metric": "rep_name"})


def test_donut_refuses_a_rate():
    """A slice of a pie has to mean a share of a whole. A rate has no whole."""
    problems = charts.validate_widget({"type": "donut", "metric": "close_rate", "group_by": "team"})
    assert any("share of a total" in p for p in problems)


def test_donut_accepts_an_additive_metric():
    widget = {"type": "donut", "metric": "net_split", "group_by": "team"}
    assert charts.validate_widget(widget) == []


def test_bad_target_is_rejected():
    assert charts.validate_widget({"type": "gauge", "metric": "net_split", "target": "soon"})


def test_build_raises_rather_than_drawing_nothing():
    with pytest.raises(charts.ChartError):
        charts.build({"type": "donut", "metric": "close_rate"}, rows())


# ---------------------------------------------------------------- computation
def test_big_number_matches_the_office_total():
    widget = charts.build({"type": "big_number", "metric": "net_split"}, rows())
    assert widget["value"] == 508_000


def test_bar_totals_each_team_through_the_shared_roll_up():
    widget = charts.build({"type": "bar", "metric": "net_split", "group_by": "team"}, rows())
    values = {s["label"]: s["value"] for s in widget["series"]}
    assert values["Alpha"] == 240_000
    assert values["Bravo"] == 260_000
    assert widget["series"][0]["label"] == "Bravo"  # sorted, highest first


def test_grouping_by_rep_lists_people():
    widget = charts.build(
        {"type": "leaders", "metric": "sold_leads", "group_by": "rep", "limit": 2}, rows()
    )
    assert [s["label"] for s in widget["series"]] == ["A", "C"]
    assert widget["hidden"] == 2


def test_donut_slices_sum_to_the_whole():
    """Trimming to the top few must not silently shrink the pie."""
    widget = charts.build(
        {"type": "donut", "metric": "net_split", "group_by": "team", "limit": 2}, rows()
    )
    assert sum(s["value"] for s in widget["series"]) == widget["total"]
    assert widget["series"][-1]["label"] == "Other"


def test_donut_without_a_remainder_has_no_other_slice():
    widget = charts.build({"type": "donut", "metric": "net_split", "group_by": "team"}, rows())
    assert all(s["label"] != "Other" for s in widget["series"])


def test_series_are_capped_even_when_a_larger_limit_is_asked_for():
    many = [
        RepRow(rep_key=f"r{i}", rep_name=f"R{i}", team=f"Team {i}",
               components={"net_split": 100 - i}).as_row()
        for i in range(20)
    ]
    widget = charts.build(
        {"type": "bar", "metric": "net_split", "group_by": "team", "limit": 99}, many
    )
    assert len(widget["series"]) == charts.MAX_SERIES
    assert widget["hidden"] == 20 - charts.MAX_SERIES


def test_gauge_carries_its_target():
    widget = charts.build({"type": "gauge", "metric": "net_split", "target": 1_000_000}, rows())
    assert widget["value"] == 508_000
    assert widget["target"] == 1_000_000


def test_trend_passes_points_through():
    points = [{"date": "2026-09-01", "value": 10}, {"date": "2026-09-02", "value": 20}]
    widget = charts.build({"type": "trend", "metric": "net_split"}, rows(), points)
    assert widget["points"] == points


def test_empty_rows_do_not_crash():
    widget = charts.build({"type": "big_number", "metric": "net_split"}, [])
    assert widget["value"] == 0


def test_catalogue_is_consistent_with_the_types():
    catalogue = charts.catalogue()
    assert {c["key"] for c in catalogue["charts"]} == set(charts.CHART_BY_KEY)
    assert catalogue["max_series"] == charts.MAX_SERIES
