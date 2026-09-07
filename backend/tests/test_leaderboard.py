from scoreboard.domain.leaderboard import (
    RepRow,
    all_groups,
    group_totals,
    group_vs_group,
    rank,
    whole_office,
)


def rows():
    def rep(name, group, issued, sold, gross, net):
        return RepRow(
            rep_key=name.lower().replace(" ", "-"),
            rep_name=name,
            group=group,
            components={
                "issued_leads": issued, "pitched_leads": issued * 0.7, "sold_leads": sold,
                "gross_split": gross, "pending_split": 0, "net_split": net,
            },
        ).as_row()

    return [
        rep("Andre Okafor", "Undisputed", 50, 20, 200_000, 180_000),
        rep("Lena Kovac", "Undisputed", 40, 12, 130_000, 120_000),
        rep("Marcus Reed", "Team Alpha", 30, 9, 90_000, 84_000),
        rep("Dana Whitfield", "Team Alpha", 20, 4, 40_000, 36_000),
    ]


def test_rank_is_always_highest_first():
    ranked = rank(rows(), "net_split")
    assert [r["rank"] for r in ranked] == [1, 2, 3, 4]
    assert ranked[0]["rep_name"] == "Andre Okafor"


def test_unknown_rank_metric_falls_back_instead_of_raising():
    ranked = rank(rows(), "not_a_metric")
    assert ranked[0]["rep_name"] == "Andre Okafor"


def test_group_total_equals_sum_of_its_members():
    totals = group_totals(rows(), "net_split")
    undisputed = next(t for t in totals if t["group"] == "Undisputed")
    assert undisputed["net_split"] == 300_000
    assert undisputed["issued_leads"] == 90
    assert undisputed["rep_count"] == 2
    assert round(undisputed["close_rate"], 4) == round(32 / 90 * 100, 4)


def test_whole_office_total_matches_every_rep():
    board = whole_office(rows(), "net_split")
    assert board["total"]["net_split"] == 420_000
    assert board["total"]["issued_leads"] == 140
    assert len(board["reps"]) == 4


def test_group_vs_group_is_capped_at_two_and_labels_a_winner():
    board = group_vs_group(rows(), ["Undisputed", "Team Alpha", "Team Bravo"], "net_split")
    assert len(board["groups"]) == 2
    assert board["groups"][0]["placement"] == "WINNER"
    assert board["groups"][1]["placement"] == ""


def test_all_groups_mvp_follows_the_ranking_metric():
    by_net = all_groups(rows(), "net_split")
    undisputed = next(t for t in by_net["groups"] if t["group"] == "Undisputed")
    assert undisputed["mvp"]["rep_name"] == "Andre Okafor"

    by_close = all_groups(rows(), "close_rate")
    alpha = next(t for t in by_close["groups"] if t["group"] == "Team Alpha")
    # Marcus closes 9/30 = 30%, Dana 4/20 = 20%.
    assert alpha["mvp"]["rep_name"] == "Marcus Reed"
