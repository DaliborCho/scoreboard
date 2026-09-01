"""Tableau parsing, against recorded exports.

These fixtures are the reason no live credential is needed to develop. Each
one encodes a failure that actually happened in the original single-office
product, so the behaviour cannot be lost in a refactor.
"""
from pathlib import Path

import pytest

from scoreboard.connectors.tableau import branch_profile, clean_number, parse_csv

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("$48,750", 48750.0),
        ("82%", 82.0),
        ("(120)", -120.0),
        ("", None),
        ("—", None),
        ("n/a", None),
        (None, None),
    ],
)
def test_clean_number(raw, expected):
    assert clean_number(raw) == expected


def test_wide_export_maps_columns_by_alias():
    records = parse_csv(load("tableau_wide.csv"))
    assert len(records) == 4

    marcus = next(r for r in records if r.rep_name == "Marcus Reed")
    assert marcus.rep_key == "marcus-reed"
    assert marcus.source_team == "Team Alpha"
    assert marcus.home_branch == "Olympia"
    assert marcus.title == "Sales Rep"
    assert marcus.components["issued_leads"] == 40
    assert marcus.components["gross_split"] == 120_000
    assert marcus.components["net_split"] == 105_000


def test_long_export_counts_leads_once_but_sums_dollars():
    """A sold lead covering two products appears on two rows.

    Counting it twice would inflate the close rate of anyone who sells
    bundles, which is precisely the rep a leaderboard is watched for.
    """
    records = parse_csv(load("tableau_long.csv"))
    marcus = next(r for r in records if r.rep_name == "Marcus Reed")

    # L-1 appears on two rows, L-2 on one: two distinct leads.
    assert marcus.components["issued_leads"] == 2
    # Dollars sum across every row: 10000 + 5000 + 20000.
    assert marcus.components["gross_split"] == 35_000
    assert marcus.components["sold_leads"] == 1


def test_rollup_rows_are_skipped():
    records = parse_csv(load("tableau_long.csv"))
    marcus = next(r for r in records if r.rep_name == "Marcus Reed")
    assert marcus.components["issued_leads"] != 999
    assert marcus.components["gross_split"] != 999_999


def test_missing_rep_column_is_a_clear_error():
    with pytest.raises(Exception) as exc:
        parse_csv("Some Column,Another\n1,2\n")
    assert "sales-rep column" in str(exc.value)


def test_branch_profile_exposes_an_ignored_filter():
    """Tableau silently ignores a filter whose key does not match a field.

    The connector detects that by inspecting what came back, so another
    office cannot quietly appear on a branch board.
    """
    column, values = branch_profile(load("tableau_wrong_branch.csv"))
    assert column == "USER-Home Branch"
    assert values == {"Olympia", "Tacoma"}


def test_explicit_mapping_overrides_aliases():
    csv_text = "SR-Name,Widgets Moved\nMarcus Reed,17\n"
    records = parse_csv(csv_text, mapping={"sold_leads": "Widgets Moved"})
    assert records[0].components["sold_leads"] == 17
