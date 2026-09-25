"""A drop point for music that did not come from a download job.

Staging was a waiting room: files went in and stayed there until beets agreed
to admit them, which for most of them was never. Alex's held 877 files and
Kelly's 327. The inbox is the opposite - it is transient. Something appears,
it stops changing, it is filed, and the inbox is empty again. At rest it holds
nothing, which means "is there anything in the inbox" is a question with an
obvious answer rather than a backlog nobody reads.

Each file is filed by its own tags, exactly as a finished download is. There
is no grouping pass and no album-versus-single decision, because there is no
longer a difference between how the two arrive.

Polled rather than watched through inotify. The inbox is meant to be written
to over a network share, and inotify does not see a write that happens on the
other end of SMB. A directory listing every few seconds costs nothing at this
size and works wherever the files come from.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import filer, uuidtags, workspace
from .config import settings

log = logging.getLogger("download_center.inbox")

# How often to look. The quiet period before a file is touched is measured in
# minutes, so this only decides how soon after that it is noticed.
POLL_SECONDS = 15


@dataclass
class Result:
    filed: list[Path] = field(default_factory=list)
    waiting: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.filed)


def settled(path: Path, quiet_seconds: int | None = None) -> bool:
    """Whether a path has stopped changing and is safe to file.

    Nothing here can know whether something is mid-copy - a file arriving
    over a network share is written by a machine this one cannot ask - so it
    waits for stillness instead. Filing half a file would put a truncated
    track in the library under a real name.
    """
    if quiet_seconds is None:
        quiet_seconds = settings.staging_quiet_seconds
    cutoff = time.time() - quiet_seconds
    try:
        newest = path.stat().st_mtime
    except OSError:
        return False
    if path.is_dir():
        for child in path.rglob("*"):
            try:
                newest = max(newest, child.stat().st_mtime)
            except OSError:
                return False
    return newest <= cutoff


def waiting(space: workspace.Workspace) -> list[Path]:
    """Audio files sitting in one person's inbox, however deeply nested.

    Nested because a folder is what gets dragged in. The folder's name is
    ignored entirely - the tags decide where each file goes, so dropping in
    an album directory and dropping in its loose tracks give the same result.
    """
    root = space.inbox_dir
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not uuidtags.is_audio(path):
            continue
        # Hidden directories are skipped; a file is judged by its suffix
        # alone. `name.startswith(".")` looked like a dotfile check and also
        # skipped two real tracks called "..." and "... (Continued)".
        if any(part.startswith(".") for part in path.relative_to(root).parts[:-1]):
            continue
        found.append(path)
    return found


def _prune(root: Path) -> None:
    """Remove directories the filing emptied, deepest first.

    The inbox is empty at rest, and a tree of empty folders left behind by a
    dragged-in album reads as "something is still in there".
    """
    if not root.is_dir():
        return
    for entry in sorted(root.rglob("*"), reverse=True):
        if entry.is_dir() and not any(entry.iterdir()):
            try:
                entry.rmdir()
            except OSError:
                pass


def drain(space: workspace.Workspace) -> Result:
    """File everything in the inbox that has stopped changing."""
    result = Result()
    for path in waiting(space):
        if not settled(path):
            result.waiting += 1
            continue
        try:
            filed = filer.file_track(space, path)
        except Exception as exc:
            result.failures.append(f"{path.name}: {type(exc).__name__}: {exc}")
            log.exception("could not file %s from %s's inbox",
                          path.name, space.username)
            continue
        result.filed.append(filed.path)

    if result.filed:
        _prune(space.inbox_dir)
        log.info("filed %d file(s) from %s's inbox",
                 len(result.filed), space.username)
    return result


def drain_all() -> dict[str, Result]:
    """Empty every workspace's inbox. Runs with nobody signed in.

    Read back off disk rather than from a session, for the same reason the
    sweep used to be: a file dropped in by hand has no job to explain it and
    no account attached, so the directory itself has to say whose it is.
    """
    results: dict[str, Result] = {}
    for space in workspace.existing():
        if not space.library_path.is_dir():
            # The library is not mounted in this container, so anything filed
            # there would be written into the container and lost on restart.
            log.warning("%s's library at %s is not mounted; leaving the "
                        "inbox alone", space.username, space.library_path)
            continue
        result = drain(space)
        if result.changed or result.failures:
            results[space.username] = result
    return results
