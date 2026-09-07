"""What version of the front end a television is running.

A wall runs for months. When the served pages change, every television is
still executing the JavaScript it downloaded on the morning somebody last
touched it, and there is nobody standing beside it to press F5.

So the board carries a marker with every poll and reloads itself when the
marker changes. The marker is a hash of the page itself rather than a process
id, deliberately: a server that restarts with unchanged code must not make ten
televisions reload, and a crash-looping container must not make them thrash.
Only a genuine change to what is served counts as a change.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

WEB = Path(__file__).parent / "web"

#: The files a television actually executes. The console and the admin panel
#: are reloaded by whoever is sitting in front of them.
WATCHED = ("tv.html",)

#: Keyed by what a cheap `stat` can see. Hashing tens of kilobytes on every
#: poll from every screen would be wasteful, and caching outright would go
#: stale in development, where the pages are a mounted volume and nothing
#: restarts the process when one changes.
_cache: dict[tuple, str] = {}


def _stamp() -> tuple:
    marks = []
    for name in WATCHED:
        try:
            info = (WEB / name).stat()
            marks.append((name, info.st_mtime_ns, info.st_size))
        except OSError:
            marks.append((name, 0, 0))
    return tuple(marks)


def build_id() -> str:
    """A short digest of the served television page."""
    stamp = _stamp()
    found = _cache.get(stamp)
    if found is not None:
        return found

    digest = hashlib.sha256()
    for name in WATCHED:
        try:
            digest.update((WEB / name).read_bytes())
        except OSError:
            # A missing page is a deployment problem, not a reason to refuse
            # to answer a television. The marker simply stops being useful.
            digest.update(name.encode())

    # One entry is the steady state; the dict only grows while somebody is
    # editing, and each edit supersedes the last.
    if len(_cache) > 8:
        _cache.clear()
    _cache[stamp] = digest.hexdigest()[:12]
    return _cache[stamp]
