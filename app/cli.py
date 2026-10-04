"""What every maintenance command checks before it starts."""

from __future__ import annotations

import os
import sys


def not_as_root(command: str) -> None:
    """Refuse to run as root, unless told to.

    `docker exec` is root by default, and these commands write into /config
    - state.db's journal, plans, beets indexes - so a run as root left files
    the app, running as its own user, could no longer write.
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0 or os.environ.get("DC_ALLOW_ROOT"):
        return
    sys.exit(f"Run this as the app's user, so what it writes stays writable:\n"
             f"  docker exec -u downloader navidrome-companion python -m app.{command} ...\n"
             f"(Set DC_ALLOW_ROOT=1 to run it as root anyway.)")
