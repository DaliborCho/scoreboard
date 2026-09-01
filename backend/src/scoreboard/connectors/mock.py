"""Deterministic fake source.

Development and demos run against this so no real credential ever has to sit
on a developer machine. The numbers are generated from a seeded RNG, so the
same period always produces the same leaderboard and screenshots stay stable.
"""
from __future__ import annotations

import random

from scoreboard.connectors.base import ConnectionResult, Connector, Period, SourceRecord

PEOPLE = [
    ("Marcus Reed", "Team Alpha", "Sales Rep"),
    ("Dana Whitfield", "Team Alpha", "Sales Rep"),
    ("Tomas Bergstrom", "Team Alpha", "Sales Rep"),
    ("Priya Raghavan", "Team Bravo", "Sales Rep"),
    ("Caleb Nunez", "Team Bravo", "Sales Rep"),
    ("Ingrid Solberg", "Team Bravo", "SMIT"),
    ("Andre Okafor", "Undisputed", "Sales Rep"),
    ("Lena Kovac", "Undisputed", "Sales Rep"),
    ("Ruth Adeyemi", "Undisputed", "Sales Rep"),
    ("Felix Moreau", "Undisputed", "Sales Rep"),
    ("Sam Trelawney", "Team Charlie", "Sales Rep"),
    ("Nadia Halvorsen", "Team Charlie", "Sales Rep"),
]


def rep_key_for(name: str) -> str:
    key = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    while "--" in key:
        key = key.replace("--", "-")
    return key or "unknown"


class MockConnector(Connector):
    kind = "mock"
    label = "Demo data"

    def test_connection(self) -> ConnectionResult:
        return ConnectionResult(ok=True, message=f"Demo source ready ({len(PEOPLE)} reps).")

    def discover(self) -> dict:
        return {
            "reports": [{"id": "demo", "name": "Demo Rep Totals"}],
            "columns": ["Rep", "Team", "Issued", "Pitched", "Sold", "Gross", "Pending", "Net"],
            "filters": [],
        }

    def fetch(self, period: Period) -> list[SourceRecord]:
        rng = random.Random(f"{period.start}:{period.end}")
        branch = str(self.config.get("branch") or "Olympia")

        records = []
        for name, team, title in PEOPLE:
            issued = rng.randint(18, 70)
            pitched = rng.randint(int(issued * 0.45), issued)
            sold = rng.randint(0, max(1, int(pitched * 0.55)))
            gross = round(sold * rng.uniform(9_500, 24_000), 2)
            pending = round(gross * rng.uniform(0.0, 0.18), 2)
            net = round(gross - pending - gross * rng.uniform(0.0, 0.07), 2)

            records.append(
                SourceRecord(
                    rep_key=rep_key_for(name),
                    rep_name=name,
                    source_team=team,
                    home_branch=branch,
                    title=title,
                    hire_date="",
                    components={
                        "issued_leads": float(issued),
                        "pitched_leads": float(pitched),
                        "sold_leads": float(sold),
                        "gross_split": gross,
                        "pending_split": pending,
                        "net_split": net,
                    },
                )
            )
        return records
