"""Long jobs that are not downloads: importing staging, auditing the disk.

Both used to run inside their request handler. Importing walks every waiting
path through beets with a 900-second timeout *per path*, and auditing reads
the tags of every file in the library. A browser gives up long before either
finishes, so the button looked broken while the work carried on invisibly -
and because both held a process-wide lock from inside a threadpool worker, a
second click did not queue politely behind the first. It occupied another of
the forty threads FastAPI has, blocking on a lock, for as long as the first
one took. Enough clicks and every `to_thread` call in the application - the
health panel, playlists, queueing a download - had nowhere to run.

So they run here instead: one at a time per name, off the request, with a
status anyone can ask for. Starting one that is already running reports the
one in flight rather than starting a second.

This deliberately does not persist. An operation that was interrupted by a
restart has no meaningful "resume" - beets either moved the files or it did
not, and the next sweep finds whatever is left.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Awaitable, Callable

log = logging.getLogger("download_center.operations")

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
    task: asyncio.Task | None = field(default=None, repr=False)

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
        }


_operations: dict[str, Operation] = {}

# Called with an Operation whenever one changes state, so the browser can be
# told without polling. Set by main.py; left unset in tests.
_on_change: Callable[[Operation], Awaitable[None]] | None = None


def subscribe(callback: Callable[[Operation], Awaitable[None]]) -> None:
    global _on_change
    _on_change = callback


def get(name: str) -> Operation:
    return _operations.setdefault(name, Operation(name))


def all_operations() -> list[dict[str, Any]]:
    return [op.as_dict() for op in _operations.values()]


async def _announce(operation: Operation) -> None:
    if _on_change is None:
        return
    try:
        await _on_change(operation)
    except Exception:
        log.debug("could not announce %s", operation.name, exc_info=True)


def start(name: str, owner: str | None,
          work: Callable[[], dict[str, Any]]) -> tuple[Operation, bool]:
    """Run `work` in a thread, off the request. Returns (operation, started).

    `started` is False when one was already in flight, in which case the
    operation returned is that one. The caller reports it rather than
    queueing a second: these are idempotent sweeps, so running one twice
    concurrently gains nothing and costs a thread.
    """
    operation = get(name)
    if operation.running:
        return operation, False

    operation.status = RUNNING
    operation.owner = owner
    operation.started_at = time.time()
    operation.finished_at = None
    operation.result = None
    operation.error = None

    async def run() -> None:
        try:
            operation.result = await asyncio.to_thread(work)
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


def reset() -> None:
    """For tests."""
    _operations.clear()
