"""Background worker process.

    docker compose up worker

Kept as a plain loop rather than a task queue. There is one job, it is
idempotent, and missing a tick costs nothing because the next one refreshes
the same period. A queue would add moving parts without adding a guarantee.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time

from scoreboard.db import session_factory
from scoreboard.services.scheduler import run_once

TICK_SECONDS = int(os.environ.get("SCOREBOARD_WORKER_TICK", "30"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
)
log = logging.getLogger("scoreboard.worker")

_running = True


def _stop(signum, _frame):
    global _running
    log.info("received signal %s, finishing current pass", signum)
    _running = False


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("worker started, tick every %ss", TICK_SECONDS)

    while _running:
        try:
            with session_factory()() as session:
                results = run_once(session)
            if results:
                ok = sum(1 for r in results if r["ok"])
                log.info("refreshed %s/%s due sources", ok, len(results))
        except Exception:
            log.exception("pass failed; continuing")

        # Sleep in short steps so shutdown is prompt rather than up to a full tick.
        for _ in range(TICK_SECONDS):
            if not _running:
                break
            time.sleep(1)

    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
