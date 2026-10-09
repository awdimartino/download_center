"""Where long blocking work runs, so it cannot starve the requests.

`asyncio.to_thread` uses the event loop's default pool, which is
`min(32, cpus + 4)` threads: eight on the Pi. Every request needs one of
those for its session lookup, and downloads, ReplayGain runs, disk audits,
resolving playlists and the background loops used to hold them too - for
minutes, or hours. Eight of those at once and every page, sign-in and the
container's healthcheck queued behind them with nothing logged.

So long work runs here instead, in a pool of its own, and the default pool
is left to the quick work requests do. What bounds the long work is not this
pool's size but the limits already on it: the download gate, one operation
per person and name, one pass of each loop at a time.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any
from collections.abc import Callable

# Generous on purpose: a thread waiting on a socket costs a little memory,
# and running out here is the same failure this module exists to prevent.
MAX_WORKERS = 32

_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="work")


def submit[T](func: Callable[..., T], /, *args: Any, **kwargs: Any) -> Future[T]:
    """Start long work on this pool without waiting for it - for work a
    request begins and a later request asks after."""
    call = functools.partial(contextvars.copy_context().run, func, *args, **kwargs)
    return _pool.submit(call)


async def run[T](func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """`asyncio.to_thread`, on the long-work pool instead of the default one.

    The caller's context variables go with it, as they do with to_thread.
    """
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, func, *args, **kwargs)
    return await loop.run_in_executor(_pool, call)
