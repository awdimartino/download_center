"""Long jobs that are not downloads: retagging, ReplayGain, combining
albums, resolving duplicates, auditing the disk.

The first two used to run inside their request handler. A retag through
beets can take minutes, and auditing reads the tags of every file in the
library. A browser gives up long before either finishes, so the button looked broken while the work carried on invisibly -
and because both held a process-wide lock from inside a threadpool worker, a
second click did not queue politely behind the first. It occupied another
thread, blocking on a lock, for as long as the first one took. Enough clicks
and every `to_thread` call in the application - the health panel,
playlists, queueing a download - had nowhere to run. The work itself now
runs on `threads`' pool, apart from the one requests use.

So they run here instead: one at a time per person and name, off the
request, with a status its owner can ask for. Starting one that is already
running reports the one in flight rather than starting a second.

Per person, not per name. Keyed by name alone, Kelly starting a combine
while Alex's ran was handed Alex's operation - hers was silently never run
and its result went to him - and every person's results, folder names and
candidate lists included, were readable by everyone.

This deliberately does not persist. An operation that was interrupted by a
restart has no meaningful "resume" - each file was either written or it was
not, and starting the same thing again from the page picks up the rest.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Awaitable, Callable

from . import threads

log = logging.getLogger("navidrome_companion.operations")

IDLE, RUNNING, DONE, FAILED = "idle", "running", "done", "failed"


@dataclass
class Operation:
    name: str
    status: str = IDLE
    owner: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    # How far through a long run it is, as the work reports it. Only the
    # work knows what a unit is - an album, a file - so it is free-form.
    progress: dict[str, Any] | None = None
    # Asked to stop. The work checks between units; nothing is interrupted
    # midway through a file.
    stop_requested: bool = False
    # What it is working on, for work aimed at one thing - which album a
    # candidate lookup is about. Without it a caller handed the operation in
    # flight cannot tell whether that is its own request or somebody else's.
    target: dict[str, Any] | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.status == RUNNING

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "owner": self.owner,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "seconds": round(
                (self.finished_at or time.time()) - self.started_at, 1)
            if self.started_at else None,
            "result": self.result,
            "error": self.error,
            "progress": self.progress,
            "stopping": self.stop_requested,
            "target": self.target,
        }


_operations: dict[tuple[str | None, str], Operation] = {}

# Called with an Operation whenever one changes state, so the browser can be
# told without polling. Set by main.py; left unset in tests.
_on_change: Callable[[Operation], Awaitable[None]] | None = None


def subscribe(callback: Callable[[Operation], Awaitable[None]]) -> None:
    global _on_change
    _on_change = callback


def get(name: str, owner: str | None) -> Operation:
    return _operations.setdefault((owner, name), Operation(name, owner=owner))


def all_operations(owner: str | None) -> list[dict[str, Any]]:
    """This person's operations, and nobody else's."""
    return [op.as_dict() for (who, _), op in _operations.items()
            if who == owner]


async def _announce(operation: Operation) -> None:
    if _on_change is None:
        return
    try:
        await _on_change(operation)
    except Exception:
        log.debug("could not announce %s", operation.name, exc_info=True)


def start(name: str, owner: str | None,
          work: Callable[[], dict[str, Any]],
          target: dict[str, Any] | None = None) -> tuple[Operation, bool]:
    """Run `work` in a thread, off the request. Returns (operation, started).

    `started` is False when one was already in flight, in which case the
    operation returned is that one. The caller reports it rather than
    queueing a second: these are idempotent sweeps, so running one twice
    concurrently gains nothing and costs a thread.
    """
    operation = get(name, owner)
    if operation.running:
        return operation, False

    operation.status = RUNNING
    operation.started_at = time.time()
    operation.finished_at = None
    operation.result = None
    operation.error = None
    operation.progress = None
    operation.stop_requested = False
    operation.target = target
    operation.loop = asyncio.get_running_loop()

    async def run() -> None:
        try:
            operation.result = await threads.run(work)
            operation.status = DONE
        except Exception as exc:
            operation.status = FAILED
            operation.error = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("operation %s failed", name)
        finally:
            operation.finished_at = time.time()
            operation.task = None
            await _announce(operation)

    operation.task = asyncio.create_task(run())
    return operation, True


def report(name: str, owner: str | None, **progress: Any) -> None:
    """Called from the work's thread: how far it has got. Announced at once.

    Without this a run of an hour looks, from the browser, exactly like one
    that has hung.
    """
    operation = get(name, owner)
    operation.progress = progress
    if operation.loop is not None and not operation.loop.is_closed():
        asyncio.run_coroutine_threadsafe(_announce(operation), operation.loop)


def stop(name: str, owner: str | None) -> Operation:
    """Ask a running operation to finish after the unit it is on."""
    operation = get(name, owner)
    if operation.running:
        operation.stop_requested = True
    return operation


def stop_all() -> list[asyncio.Task]:
    """Ask every running operation to finish, for a shutdown. Returns
    their tasks, to wait on."""
    tasks = []
    for operation in _operations.values():
        if operation.running and operation.task is not None:
            operation.stop_requested = True
            tasks.append(operation.task)
    return tasks


def stopping(name: str, owner: str | None) -> bool:
    """For the work to check between units."""
    return get(name, owner).stop_requested


def reset() -> None:
    """For tests."""
    _operations.clear()
