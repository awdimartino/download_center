"""The one way music gets into the library.

Everything arrives here: a finished download, a folder dragged in over the
network share, a file copied in by hand. There is no second road. That is the
whole point - the album-versus-single routing, the completeness check and the
regrouping pass all existed because a download and a hand-drop took different
paths in, and every one of them was a place a track could end up somewhere
nobody would look for it.

Staging was a waiting room: files went in and stayed there until beets agreed
to admit them, which for most of them was never. Alex's held 877 files and
Kelly's 327. The inbox is the opposite - it is transient. Something appears,
it is filed by its own tags, and the inbox is empty again. At rest it holds
nothing, which means "is anything waiting" has an obvious answer rather than
a backlog nobody reads.

Two ways in, one door. A download is built in `.incomplete/`, tagged, then
delivered - moved into the inbox and filed on the spot, because the worker
knows that file is finished. Anything else is found by the poller, which is
also what catches a download the application died in the middle of: the file
is already in the inbox, so a restart files it rather than losing it.

Polled rather than watched through inotify. The inbox is meant to be written
to over a network share, and inotify does not see a write that happens on the
other end of SMB. A directory listing every few seconds costs nothing at this
size and works wherever the files come from.
"""

from __future__ import annotations

import logging
import shutil
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


# --- a download on its way in -----------------------------------------------
#
# Built inside the inbox rather than beside it, so arriving is a rename on one
# filesystem. Hidden, so the poller walks past a file yt-dlp is still writing.

def scratch_root(space: workspace.Workspace, job_id: str) -> Path:
    return space.incomplete_dir / job_id


def scratch_path(space: workspace.Workspace, job_id: str, item_id: str) -> Path:
    """Where one item is built. Named for the item, not for its tags.

    The final name comes from the tags the download is given afterwards, and
    those are not known until it has been fetched. Item ids are generated
    hex, so this is always a legal filename.
    """
    return scratch_root(space, job_id) / f"{item_id}.mp3"


def discard(space: workspace.Workspace, job_id: str) -> None:
    """Throw away a job's unfinished downloads.

    Only ever the scratch directory. A track that was already delivered is in
    the library and is not this function's business - cancelling a job does
    not un-download what it finished.
    """
    shutil.rmtree(scratch_root(space, job_id), ignore_errors=True)


def deliver(space: workspace.Workspace, source: Path) -> filer.Filed:
    """Put a finished download into the inbox, and file it straight away.

    The move is what makes this the same road everything else takes; filing
    it immediately is only the worker saying so, rather than waiting for the
    poller to work out what it already knows.

    The two cannot collide over the same file. A rename keeps the file's
    mtime, so what lands here is seconds old and `settled` is false for it
    for the whole quiet period - by which time this has long since filed it.
    And if the application dies in between, that same quiet period is what
    hands the file to the poller on the next start instead of losing it.
    """
    space.inbox_dir.mkdir(parents=True, exist_ok=True)
    arrived = _move_in(source, space.inbox_dir / source.name)
    return filer.file_track(space, arrived)


def _move_in(source: Path, target: Path) -> Path:
    """Move into the inbox without overwriting something already waiting."""
    if target.exists():
        stem, suffix = target.stem, target.suffix
        for n in range(2, 100):
            candidate = target.with_name(f"{stem} ({n}){suffix}")
            if not candidate.exists():
                target = candidate
                break
        else:
            raise FileExistsError(
                f"{target} and 98 numbered variants all exist; refusing to "
                f"overwrite. Clear some out of {target.parent}.")
    shutil.move(str(source), str(target))
    return target


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
        if not entry.is_dir():
            continue
        # Never the scratch tree. It is empty between jobs and removing any
        # of it would race a download about to write in there.
        if any(part.startswith(".")
               for part in entry.relative_to(root).parts):
            continue
        if not any(entry.iterdir()):
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
