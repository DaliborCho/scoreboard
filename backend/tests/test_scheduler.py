"""Refresh scheduling.

The decision "should this run now" is pure, so these tests move the clock
instead of waiting on it.
"""
from datetime import datetime, timedelta, timezone

from scoreboard.models import DataSource
from scoreboard.services.scheduler import MIN_INTERVAL_SECONDS, is_due

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def source(**overrides) -> DataSource:
    values = {
        "org_id": 1, "kind": "mock", "name": "Test source",
        "config": {}, "secret_encrypted": "", "is_enabled": True,
        "refresh_seconds": 900, "last_run_at": None,
    }
    values.update(overrides)
    return DataSource(**values)


def test_a_source_that_has_never_run_is_due():
    assert is_due(source(), NOW)


def test_a_source_inside_its_interval_is_not_due():
    assert not is_due(source(last_run_at=NOW - timedelta(seconds=300)), NOW)


def test_a_source_past_its_interval_is_due():
    assert is_due(source(last_run_at=NOW - timedelta(seconds=901)), NOW)


def test_disabled_sources_never_run():
    assert not is_due(source(is_enabled=False), NOW)
    assert not is_due(source(is_enabled=False, last_run_at=None), NOW)


def test_push_sources_are_never_polled():
    """Nothing to pull from a customer who sends data to us."""
    assert not is_due(source(kind="ingest"), NOW)


def test_unknown_kind_is_skipped_rather_than_raising():
    """One bad row must not take down the pass for every other customer."""
    assert not is_due(source(kind="something-we-removed"), NOW)


def test_interval_floor_protects_the_customer_system():
    """A zero interval must not turn into a request loop against Tableau."""
    eager = source(refresh_seconds=0, last_run_at=NOW - timedelta(seconds=30))
    assert not is_due(eager, NOW)
    assert is_due(
        source(refresh_seconds=0, last_run_at=NOW - timedelta(seconds=MIN_INTERVAL_SECONDS)), NOW
    )


def test_exactly_at_the_interval_counts_as_due():
    assert is_due(source(refresh_seconds=900, last_run_at=NOW - timedelta(seconds=900)), NOW)
