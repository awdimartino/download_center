"""How the background loops are doing, for Health to show.

The three loops - filing the inbox, reading play counts, auditing the disk -
catch everything so that one bad pass cannot end them. That also meant a
loop failing every pass, for days, said so only in the log: an unreadable
workspace marker stopped every inbox from being filed while the page looked
exactly as it always did.

Each loop records how its last pass went here. Nothing is persisted: after
a restart a loop has simply not run yet.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class Beat:
    ok_at: float | None = None       # the last pass that completed
    failed_at: float | None = None   # the last pass that raised
    error: str | None = None
    # A pass can complete and still have something to say: a workspace it
    # could not read, files it could not file.
    problem: str | None = None


_beats: dict[str, Beat] = {}
_lock = threading.Lock()


def ok(name: str, problem: str | None = None) -> None:
    with _lock:
        beat = _beats.setdefault(name, Beat())
        beat.ok_at = time.time()
        beat.error = None
        beat.problem = problem


def failed(name: str, exc: BaseException) -> None:
    with _lock:
        beat = _beats.setdefault(name, Beat())
        beat.failed_at = time.time()
        beat.error = f"{type(exc).__name__}: {exc}"[:300]


def snapshot() -> dict[str, dict[str, Any]]:
    with _lock:
        return {name: vars(beat).copy() for name, beat in _beats.items()}


def reset() -> None:
    """For tests."""
    with _lock:
        _beats.clear()
