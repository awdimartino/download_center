"""Results kept until the data under them changes.

The listening statistics are a pure function of the play history and the
track index, and both change far less often than somebody opens Home: the
history moves when a reading catches a new play, the index when Navidrome
scans. Recomputing every statistic on every visit cost seconds on a Pi for
an answer that was almost always the same as last time.

So each result is stored beside the version of the data it was computed
from, and a lookup whose version still matches is answered without doing
the work. Nothing is ever invalidated by hand: a caller that forgot to would
serve a stale answer silently, and a version that has moved cannot be
forgotten.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any

# Enough for every statistic of a couple of accounts across the ranges the
# Listening panel offers. Past it the least recently used goes.
LIMIT = 128

_lock = threading.Lock()
_entries: OrderedDict[Hashable, tuple[Hashable, Any]] = OrderedDict()
# One lock per key, so Home's two requests arriving together compute a
# shared piece once - the second waits for the first and takes its answer -
# while unrelated keys still run side by side.
_computing: dict[Hashable, threading.Lock] = {}


def _lookup(key: Hashable, version: Hashable) -> tuple[bool, Any]:
    with _lock:
        hit = _entries.get(key)
        if hit is not None and hit[0] == version:
            _entries.move_to_end(key)
            return True, hit[1]
    return False, None


def cached[T](key: Hashable, version: Hashable, compute: Callable[[], T]) -> T:
    """`compute()`, or what it returned last time if `version` is unchanged.

    The value is shared between callers, so it must be treated as read-only.
    """
    found, value = _lookup(key, version)
    if found:
        return value
    with _lock:
        guard = _computing.setdefault(key, threading.Lock())
    with guard:
        found, value = _lookup(key, version)
        if found:
            return value
        value = compute()
        with _lock:
            _entries[key] = (version, value)
            _entries.move_to_end(key)
            while len(_entries) > LIMIT:
                old, _ = _entries.popitem(last=False)
                _computing.pop(old, None)
    return value


def clear() -> None:
    with _lock:
        _entries.clear()
        _computing.clear()
