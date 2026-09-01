"""Throttling.

Times are passed in, so the window can be crossed without waiting through it.
"""
import pytest

from scoreboard.services import ratelimit
from scoreboard.services.ratelimit import RateLimited


@pytest.fixture(autouse=True)
def clean():
    ratelimit.reset_all()
    yield
    ratelimit.reset_all()


def fail(bucket, times, limit=3, window=60, start=1000.0, step=1.0):
    for i in range(times):
        ratelimit.record(bucket, window, now=start + i * step)


def test_an_empty_bucket_passes():
    ratelimit.check("fresh", limit=3, window=60, now=1000.0)


def test_the_bucket_fills_and_then_refuses():
    fail("b", 3)
    with pytest.raises(RateLimited):
        ratelimit.check("b", limit=3, window=60, now=1003.0)


def test_below_the_limit_still_passes():
    fail("b", 2)
    ratelimit.check("b", limit=3, window=60, now=1003.0)


def test_check_alone_never_fills_the_bucket():
    """Separating check from record is what lets successes go uncounted."""
    for _ in range(50):
        ratelimit.check("b", limit=3, window=60, now=1000.0)
    assert ratelimit.count("b", window=60, now=1000.0) == 0


def test_old_attempts_fall_out_of_the_window():
    fail("b", 3, start=1000.0)
    with pytest.raises(RateLimited):
        ratelimit.check("b", limit=3, window=60, now=1005.0)
    # Well past the window, the same bucket is clear again.
    ratelimit.check("b", limit=3, window=60, now=1200.0)


def test_retry_after_counts_down_toward_the_oldest_attempt():
    fail("b", 3, start=1000.0, step=0.0)
    with pytest.raises(RateLimited) as caught:
        ratelimit.check("b", limit=3, window=60, now=1030.0)
    assert 25 <= caught.value.retry_after <= 31


def test_clearing_forgives_earlier_mistakes():
    """A person who mistypes then succeeds starts clean."""
    fail("b", 2)
    ratelimit.clear("b")
    assert ratelimit.count("b", window=60, now=1010.0) == 0
    fail("b", 2, start=1010.0)
    ratelimit.check("b", limit=3, window=60, now=1012.0)


def test_buckets_are_independent():
    fail("one", 3)
    with pytest.raises(RateLimited):
        ratelimit.check("one", limit=3, window=60, now=1003.0)
    ratelimit.check("two", limit=3, window=60, now=1003.0)


def test_the_address_limit_is_far_looser_than_the_account_limit():
    """A company behind one NAT must not be shut out by one person's typo.

    This relationship is the whole point of having two buckets; if it ever
    inverts, ten failures would lock out an office.
    """
    assert ratelimit.LOGIN_IP_LIMIT >= ratelimit.LOGIN_LIMIT * 5
