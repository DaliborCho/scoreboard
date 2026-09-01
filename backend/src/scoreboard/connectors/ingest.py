"""Push source: the customer sends us data instead of us fetching it.

This is the escape hatch that makes the platform sellable. A customer whose
reporting system sits behind a firewall, or which we have never heard of, can
still be onboarded: their IT posts normalized rows to the ingest endpoint with
an API key. Nothing of theirs has to be reachable from our network, and no
credential of theirs has to live in our database.

It is listed as a source kind so the settings screen can treat it like any
other, but it is never polled.
"""
from __future__ import annotations

from scoreboard.connectors.base import ConnectionResult, Connector, Period, SourceRecord


class IngestConnector(Connector):
    kind = "ingest"
    label = "Push API"
    pullable = False

    def test_connection(self) -> ConnectionResult:
        return ConnectionResult(
            ok=True,
            message="Waiting for pushed data. Create an API key and POST to /api/v1/ingest/reps.",
        )

    def fetch(self, period: Period) -> list[SourceRecord]:
        return []
