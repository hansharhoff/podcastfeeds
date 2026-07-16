"""Background-task tracking.

asyncio only keeps a *weak* reference to tasks created with
``asyncio.create_task``; under GC pressure a task can be collected before it
finishes, silently aborting whatever it was doing (here: episode processing).
``spawn`` keeps a strong reference until the task completes.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine

log = logging.getLogger("podcastfeeds")

_background: set[asyncio.Task] = set()


def spawn(coro: Coroutine, *, name: str | None = None) -> asyncio.Task:
    """Schedule ``coro`` on the event loop and retain a strong reference to the
    task until it finishes, logging any unhandled exception."""
    task = asyncio.create_task(coro, name=name)
    _background.add(task)

    def _done(t: asyncio.Task) -> None:
        _background.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.error("background task %s failed", t.get_name(), exc_info=t.exception())

    task.add_done_callback(_done)
    return task
