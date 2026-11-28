"""The hold expiry worker: expires stale pending holds and releases their cents.

Run it with `uv run python -m authz.workers.hold_expiry` (or the
`authz-hold-expiry` script). Every hold_expiry_interval_seconds it expires
pending holds older than hold_expiry_seconds, hold_expiry_batch_size at a
time (see settings.py). The business logic, and why several copies can run at
once safely, is in services/holds.py: expire_holds().

This module only schedules: it opens a session per batch, loops, logs, and
stops cleanly on SIGTERM or Ctrl-C.
"""

import logging
import signal
import threading
from types import FrameType

from sqlalchemy import Engine
from sqlmodel import Session

from authz.db import get_engine
from authz.services.holds import expire_holds
from authz.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def run_once(engine: Engine, settings: Settings) -> int:
    """Expire every hold that is due right now, one batch per transaction.

    Keeps going while batches come back full, so a backlog bigger than one
    batch is cleared now rather than 100 holds a minute. Each batch gets its
    own short session, so its locks are released as soon as it commits.
    Returns the total number of holds expired.
    """
    total = 0
    while True:
        with Session(engine) as session:
            count = expire_holds(
                session,
                older_than_seconds=settings.hold_expiry_seconds,
                limit=settings.hold_expiry_batch_size,
            )
        total += count
        if count < settings.hold_expiry_batch_size:
            return total


def run(engine: Engine, settings: Settings, stop: threading.Event) -> None:
    """Call run_once every interval until `stop` is set.

    stop.wait(timeout) sleeps like time.sleep, but returns straight away when
    another thread (here, the signal handler) sets the event, so shutdown
    doesn't wait out the interval. A failed run is logged and retried on the
    next tick: its transaction was rolled back, so nothing was half done.
    """
    while not stop.is_set():
        try:
            expired = run_once(engine, settings)
            if expired:
                logger.info("expired %d holds", expired)
        except Exception:
            logger.exception("hold expiry run failed; retrying next interval")
        stop.wait(settings.hold_expiry_interval_seconds)


def main() -> None:
    """Entry point: run the worker until SIGTERM or SIGINT."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = get_settings()
    stop = threading.Event()

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("received %s, stopping", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    logger.info(
        "hold expiry worker started: window %ds, every %gs, batches of %d",
        settings.hold_expiry_seconds,
        settings.hold_expiry_interval_seconds,
        settings.hold_expiry_batch_size,
    )
    run(get_engine(), settings, stop)


# `python -m authz.workers.hold_expiry` runs the module as __main__.
if __name__ == "__main__":
    main()
