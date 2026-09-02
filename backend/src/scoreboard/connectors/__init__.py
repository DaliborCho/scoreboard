"""Connector registry.

Adding a customer's system means adding one module and one line here.
"""
from __future__ import annotations

from scoreboard.connectors.base import (
    ConnectionResult,
    Connector,
    Period,
    SourceError,
    SourceRecord,
)
from scoreboard.connectors.ingest import IngestConnector
from scoreboard.connectors.mock import MockConnector
from scoreboard.connectors.rest import RestConnector
from scoreboard.connectors.tableau import TableauConnector

REGISTRY: dict[str, type[Connector]] = {
    MockConnector.kind: MockConnector,
    IngestConnector.kind: IngestConnector,
    TableauConnector.kind: TableauConnector,
    RestConnector.kind: RestConnector,
}


def build(kind: str, config: dict | None = None, secret: str = "") -> Connector:
    connector = REGISTRY.get(kind)
    if connector is None:
        raise SourceError(f"Unknown source kind '{kind}'. Known: {', '.join(sorted(REGISTRY))}")
    return connector(config=config, secret=secret)


def catalogue() -> list[dict]:
    return [
        {"kind": c.kind, "label": c.label, "pullable": c.pullable}
        for c in REGISTRY.values()
    ]


__all__ = [
    "REGISTRY",
    "ConnectionResult",
    "Connector",
    "Period",
    "SourceError",
    "SourceRecord",
    "build",
    "catalogue",
]
