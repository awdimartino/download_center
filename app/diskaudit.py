"""Auditing identity tags on disk, rather than through Navidrome's index.

Navidrome's view and the files themselves can disagree, and the disagreement
is not academic. Stamping a file preserves its mtime on purpose, so that
tagging an entire library does not look to a scanner like the entire library
changed. The cost is that an incremental scan then never re-reads those files:
the tags are on disk and Navidrome does not know it.

Reading the database alone cannot tell "never stamped" from "stamped but not
yet scanned", and those need opposite responses - run the stamper, or run a
full scan. So this walks the files.

It is slow enough that it cannot happen inside a request. The result is cached
with the time it was taken, refreshed on a timer, and can be asked for.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import uuidtags
from .config import settings

log = logging.getLogger(__name__)

# The walk is IO-bound and idempotent, so a stale result is never wrong, only
# old. Six hours is often enough to catch a bad import the same day.
REFRESH_SECONDS = 6 * 60 * 60


@dataclass
class Audit:
    files: int = 0
    stamped: int = 0
    missing_track_uuid: list[str] = field(default_factory=list)
    missing_album_uuid: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    split_albums: list[str] = field(default_factory=list)
    duplicate_uuids: list[str] = field(default_factory=list)
    untaggable: int = 0
    seconds: float = 0.0
    taken_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        # Only a sample of each list travels to the browser; the counts are
        # what the panel shows, and a few examples are enough to act on.
        return {
            "files": self.files,
            "stamped": self.stamped,
            "untaggable": self.untaggable,
            "missing_track_uuid": len(self.missing_track_uuid),
            "missing_album_uuid": len(self.missing_album_uuid),
            "unreadable": len(self.unreadable),
            "split_albums": len(self.split_albums),
            "duplicate_uuids": len(self.duplicate_uuids),
            "examples": {
                "missing_track_uuid": self.missing_track_uuid[:10],
                "unreadable": self.unreadable[:10],
                "split_albums": self.split_albums[:10],
                "duplicate_uuids": self.duplicate_uuids[:10],
            },
            "seconds": round(self.seconds, 1),
            "taken_at": self.taken_at,
        }


_cache: Audit | None = None
_lock = threading.Lock()


def run(root: Path | None = None) -> Audit:
    """Walk the library and report what identity tags are actually present."""
    root = root or settings.music_dir
    started = time.time()
    audit = Audit()

    by_uuid: dict[str, int] = collections.Counter()
    by_directory: dict[Path, set[str]] = collections.defaultdict(set)

    if not root.exists():
        audit.taken_at = started
        return audit

    for path in root.rglob("*"):
        if not path.is_file() or not uuidtags.is_audio(path):
            continue
        relative = str(path.relative_to(root))

        if not uuidtags.can_carry_tags(path):
            # A .wav has nowhere to put the tag. Counted, never complained
            # about, since the only fix is to stop using the format.
            audit.untaggable += 1
            continue

        audit.files += 1
        try:
            track_uuid, album_uuid = uuidtags.read(path)
        except uuidtags.UnreadableFile as exc:
            audit.unreadable.append(f"{relative}  ({exc})")
            continue

        if track_uuid:
            audit.stamped += 1
            by_uuid[track_uuid] += 1
        else:
            audit.missing_track_uuid.append(relative)

        if album_uuid:
            by_directory[path.parent].add(album_uuid)
        else:
            audit.missing_album_uuid.append(relative)

    # One directory is one album. Two album UUIDs in a directory means two
    # copies of the same record that Navidrome will keep showing separately,
    # however tidy the folder looks.
    audit.split_albums = [
        str(directory.relative_to(root))
        for directory, uuids in sorted(by_directory.items()) if len(uuids) > 1
    ]
    # A UUID on two files means the tag was copied rather than generated -
    # both would collapse into one track.
    audit.duplicate_uuids = [u for u, n in by_uuid.items() if n > 1]

    audit.seconds = time.time() - started
    audit.taken_at = time.time()
    log.info("disk audit: %d files in %.1fs, %d unstamped, %d split albums",
             audit.files, audit.seconds, len(audit.missing_track_uuid),
             len(audit.split_albums))
    return audit


def refresh() -> Audit:
    """Run an audit and cache it. Concurrent callers share one walk."""
    global _cache
    with _lock:
        _cache = run()
        return _cache


def cached() -> Audit | None:
    return _cache


def stale() -> bool:
    return _cache is None or (time.time() - _cache.taken_at) > REFRESH_SECONDS
