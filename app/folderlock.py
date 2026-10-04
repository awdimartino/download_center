"""One change at a time per album folder.

Renaming, matching, covers, combining, quarantining and ReplayGain all
rewrite or move the files in a folder, from worker threads, and a ReplayGain
run lasts hours. Nothing used to stop two of them meeting: rsgain and mutagen
writing one file together, or rsgain losing a file the filer had just moved.

Refused rather than queued. Every caller is a person who pressed a button or
a run that can skip the folder and come back to it; waiting would hold a
request thread for as long as the other change takes, which is what the
operations module exists to avoid.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_held: set[Path] = set()
_lock = threading.Lock()


class Busy(Exception):
    """Something else is changing that folder right now."""


def _overlaps(one: Path, other: Path) -> bool:
    return one == other or one in other.parents or other in one.parents


@contextmanager
def holding(*folders: Path) -> Iterator[None]:
    """Hold these folders for the length of the block, or raise Busy.

    A folder overlaps its parents and children too: a combine holding an
    album and a track edit inside it are the same files.
    """
    keys = {folder.resolve() for folder in folders}
    with _lock:
        for key in sorted(keys):
            if any(_overlaps(key, held) for held in _held):
                raise Busy(f"{key.name} is being changed by something else "
                           "right now; try again when that finishes.")
        _held.update(keys)
    try:
        yield
    finally:
        with _lock:
            _held.difference_update(keys)
