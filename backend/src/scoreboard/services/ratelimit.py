"""Throttling for the endpoints worth guessing at.

In-process counters. With one API container that is exactly right; with
several it becomes per-container, which weakens the limit but never breaks
correctness. Swapping the store for Redis is a change to this file alone.

Two decisions here were learned the hard way rather than designed up front:

**Only failures are counted.** Charging a successful sign-in against the
budget means a busy office slowly locks itself out during a normal morning.

**The address limit is far looser than the account limit.** A whole company
behind one NAT — or every user behind one reverse proxy — shares an address.
Ten failures by one person must not shut out their colleagues. The tight limit
belongs on the account being guessed at, which is what an attacker actually
has to iterate.
"""
from __future__ import annotations

import threading
import time
from collections import deque

_lock = threading.Lock()
_hits: dict[str, deque[float]] = {}

# Tight: this is the bucket an attacker cannot avoid filling.
LOGIN_LIMIT = 10
LOGIN_WINDOW = 300

# Loose: shared by everyone behind one address, so it exists to stop a flood,
# not to police a person.
LOGIN_IP_LIMIT = 100
LOGIN_IP_WINDOW = 300

INGEST_LIMIT = 60
INGEST_WINDOW = 60


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"Too many attempts. Try again in {retry_after} seconds.")
        self.retry_after = retry_after


def _prune(hits: deque[float], cutoff: float) -> None:
    while hits and hits[0] <= cutoff:
        hits.popleft()


def check(bucket: str, limit: int, window: int, now: float | None = None) -> None:
    """Raise if the bucket is already full. Records nothing.

    Separated from `record` so an endpoint can reject early and then decide,
    after doing the work, whether this attempt deserves to be counted.
    """
    now = time.monotonic() if now is None else now
    with _lock:
        hits = _hits.get(bucket)
        if not hits:
            return
        _prune(hits, now - window)
        if len(hits) >= limit:
            raise RateLimited(retry_after=max(1, int(window - (now - hits[0]))))


def record(bucket: str, window: int, now: float | None = None) -> None:
    """Count one failed attempt against a bucket."""
    now = time.monotonic() if now is None else now
    with _lock:
        hits = _hits.setdefault(bucket, deque())
        _prune(hits, now - window)
        hits.append(now)


def clear(bucket: str) -> None:
    """Forget a bucket after the caller proves who they are.

    Without this, someone who mistyped four times and then got it right would
    still be part-way to a lockout for the rest of the window.
    """
    with _lock:
        _hits.pop(bucket, None)


def count(bucket: str, window: int, now: float | None = None) -> int:
    now = time.monotonic() if now is None else now
    with _lock:
        hits = _hits.get(bucket)
        if not hits:
            return 0
        _prune(hits, now - window)
        return len(hits)


def reset_all() -> None:
    with _lock:
        _hits.clear()
