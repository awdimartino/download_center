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
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import filer, uuidtags, workspace
from .config import settings

log = logging.getLogger("navidrome_companion.inbox")

# How often to look. The quiet period before a file is touched is measured in
# minutes, so this only decides how soon after that it is noticed.
POLL_SECONDS = 15

# Files a worker has moved in and is filing right now.
#
# The quiet period makes the poller ignore them - a rename keeps the mtime,
# so a delivered file is seconds old - but that is a property of a *setting*,
# and `inbox_quiet_seconds` is allowed to be 0. At 0 the poller and the
# worker race for the same file and one of them loses it mid-move. This is
# the fact itself rather than a consequence of it.
_delivering: set[Path] = set()
_delivering_lock = threading.Lock()

# Library folders the inbox has just filed a track into, and when. What a
# Library edit has to wait for is music still arriving in that album - not
# "anything modified lately", which every edit is: fixing a title and then
# its track number used to be refused as "still arriving" for two minutes.
_arrivals: dict[Path, float] = {}
_arrivals_lock = threading.Lock()


def _arrived(folder: Path) -> None:
    with _arrivals_lock:
        _arrivals[folder.resolve()] = time.time()


def receiving(path: Path) -> bool:
    """Whether the inbox filed a track into this album folder (or the folder
    holding this track) within the quiet period - so more may be on the way.

    Only the inbox's own deliveries count. The app's edits, covers and
    ReplayGain no longer lock an album; a file copied straight into a
    library folder by hand is not noticed, since the inbox is the way in.
    """
    folder = (path if path.is_dir() else path.parent).resolve()
    cutoff = time.time() - settings.inbox_quiet_seconds
    with _arrivals_lock:
        for where, when in list(_arrivals.items()):
            if when < cutoff:
                del _arrivals[where]
            elif where == folder or folder in where.parents:
                return True
    return False


@dataclass
class Result:
    filed: list[Path] = field(default_factory=list)
    waiting: int = 0
    failures: list[str] = field(default_factory=list)
    # The drain itself raised: nothing in this workspace was looked at.
    broken: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.filed)


# Files that could not be filed, and the size they were when that was tried.
#
# Without this, one unfilable file is retried every POLL_SECONDS for ever -
# 5,760 attempts a day, each logging a full traceback, none of it visible to
# anybody. Keyed on the size so that changing the file asks the question
# again, which is the reasoning the deleted refusal table used: alter the
# thing and it is a different question. The message is kept with it: a file
# that is not retried is still a failure, and reporting it as "waiting" for
# ever - with the reason only in the log - was the next silence (M18).
_unfilable: dict[Path, tuple[int, str]] = {}

# Library roots already reported as missing, so a 15-second poll does not
# repeat a static fact 5,760 times a day.
_unmounted: set[Path] = set()


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


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


def clear_scratch() -> int:
    """Throw away every half-built download. Run once, at start-up.

    Jobs live in memory, so after a restart nothing will ever finish what
    is in `.incomplete/` or discard it - a crash mid-download left those
    folders there for good. A finished download is never in here: it has
    already been delivered into the inbox, where the poller files it.
    Returns how many job folders went.
    """
    cleared = 0
    for space in workspace.existing():
        try:
            leftovers = list(space.incomplete_dir.iterdir())
        except OSError:
            continue
        for path in leftovers:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            cleared += 1
    if cleared:
        log.info("cleared %d unfinished download(s) left by the last run", cleared)
    return cleared


def deliver(space: workspace.Workspace, source: Path) -> filer.Filed:
    """Put a finished download into the inbox, and file it straight away.

    The move is what makes this the same road everything else takes; filing
    it immediately is only the worker saying so, rather than waiting for the
    poller to work out what it already knows.

    The two cannot collide over the same file: it is claimed in
    `_delivering` before it arrives, and the poller skips anything claimed.
    The quiet period alone is not enough, since it can be set to 0. If the
    application dies in between, the claim dies with it, and the poller
    files the file on the next start instead of losing it.

    If filing fails, the file goes back where it was built. Left in the
    inbox, the poller filed it a couple of minutes after the item had been
    marked failed - and Retry then fetched and filed it a second time.
    """
    space.inbox_dir.mkdir(parents=True, exist_ok=True)
    # Claimed before the move, not after: in between, at a quiet period of
    # 0, the poller could see the file and start filing it too.
    arrived = filer.unused_name(space.inbox_dir / source.name)
    with _delivering_lock:
        _delivering.add(arrived)
    try:
        shutil.move(str(source), str(arrived))
        filed = filer.file_track(space, arrived)
    except Exception:
        if arrived.exists():
            try:
                if source.exists():
                    # The move itself failed partway; the original is whole.
                    arrived.unlink()
                else:
                    shutil.move(str(arrived), str(source))
            except OSError:
                log.exception("could not take %s back out of the inbox; the "
                              "poller will file it", arrived.name)
        raise
    else:
        _arrived(filed.path.parent)
        return filed
    finally:
        with _delivering_lock:
            _delivering.discard(arrived)


def _move_in(source: Path, target: Path) -> Path:
    """Move into the inbox without overwriting something already waiting.

    The numbering rule is the filer's - see `filer.unused_name` - because
    the name settled here is the name the filer then has to collide with.
    """
    target = filer.unused_name(target)
    shutil.move(str(source), str(target))
    return target


# --- a browser upload, the third way in --------------------------------------
#
# A download knows it is finished because the worker built it; an SMB drop
# can only be watched until it stops changing. A browser upload is a third
# thing: the request itself says when every byte has arrived, so there is
# nothing to watch for - but the transfer can carry several files at once,
# a whole album folder among them, and those have to land together or the
# cover art has nowhere to be found when the first track is filed.

def upload_root(space: workspace.Workspace, batch: str) -> Path:
    """One browser upload's own folder inside the inbox.

    Shared by every file the browser sends in that drop, so a folder's cover
    art is still sitting beside its tracks when `file_track` goes looking for
    one - the same thing `_carry_cover` already does for a folder dragged in
    over the network share. Delivering each file straight to the inbox root,
    the way a download does, would put every drop's cover art in one place,
    and the first track from any album to file would carry whichever cover
    happened to be there.
    """
    return space.inbox_dir / f"upload-{batch}"


def backdate(path: Path) -> None:
    """Mark a file the caller knows is finished as already settled.

    `settled()` exists because nothing here can tell an SMB copy still in
    progress from one that is done. An HTTP upload is not that: the request
    handler only reaches this once every byte has been received, so making
    it wait out the quiet period a second time would just be a several-minute
    lie about how long filing an upload takes.
    """
    age = time.time() - settings.inbox_quiet_seconds - 5
    os.utime(path, (age, age))


def release(space: workspace.Workspace, batch: str) -> None:
    """Mark one finished browser drop as settled, all of it at once.

    Called when the browser says the drop is complete. Backdating each file
    as it landed let the poller file an album's tracks before its cover had
    uploaded; this is the moment every byte of the drop is known to be here.
    """
    root = upload_root(space, batch)
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_file():
            backdate(path)


def settled(path: Path) -> bool:
    """Whether a path has stopped changing and is safe to file.

    Nothing here can know whether something is mid-copy - a file arriving
    over a network share is written by a machine this one cannot ask - so it
    waits for stillness instead. Filing half a file would put a truncated
    track in the library under a real name.
    """
    cutoff = time.time() - settings.inbox_quiet_seconds
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
    with _delivering_lock:
        return [path for path in found if path not in _delivering]


def loose_in_library(space: workspace.Workspace) -> list[Path]:
    """Audio sitting at the very top of the library, in no album folder.

    The filer never produces one of these - every file it writes goes to
    `$albumartist/$album/`. So one at the root was put there by hand, or by
    something that ran before any of this existed, and it has never been
    filed by anything.

    It cannot be left alone either: with no folder there is no album for the
    review page to offer, and a matcher handed its "folder" would be handed
    the whole library. Filing it by its tags gives it somewhere to be.

    Only the top level, never recursing. Everything below it is already in a
    folder, and `duplicates-removed/` is a directory, so it is passed over.
    """
    root = space.library_path
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir()
                  if path.is_file() and uuidtags.is_audio(path))


# What an album drop leaves once its audio has been filed: art, cue sheets,
# rip logs, playlists and checksums. Anything not on this list is left alone
# (and so is the folder holding it), because the inbox cannot know what an
# unfamiliar file is for.
RESIDUE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".cue", ".nfo",
           ".log", ".txt", ".m3u", ".m3u8", ".sfv", ".md5", ".ffp",
           ".accurip", ".db", ".ini"}


def _is_residue(path: Path) -> bool:
    return path.suffix.lower() in RESIDUE or path.name == ".DS_Store"


def _set_aside(path: Path, root: Path, leftovers: Path) -> None:
    """Move one leftover out of the inbox, keeping its relative path."""
    target = filer.unused_name(leftovers / path.relative_to(root))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(target))


def _clear_residue(space: workspace.Workspace) -> None:
    """Move what filing left behind out of the inbox, once it has settled.

    Covers, cue sheets and the like stayed in the inbox for ever, and the
    folders holding them were never pruned - so it was never empty at rest,
    and a cover.jpg left at the top was carried into every later download
    (CODE_REVIEW H7, M17). Moved, not deleted: they are somebody's files.
    Only settled, known residue goes, and only where no audio is left to
    file.
    """
    root = space.inbox_dir
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_file():
                if _is_residue(entry) and settled(entry):
                    _set_aside(entry, root, space.leftovers_dir)
                continue
            if not settled(entry):
                continue
            inside = [p for p in entry.rglob("*") if p.is_file()]
            if inside and all(_is_residue(p) for p in inside):
                for path in inside:
                    _set_aside(path, root, space.leftovers_dir)
        except OSError:
            log.warning("could not move leftovers out of %s", entry,
                        exc_info=True)


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


# One drain at a time. Upload-finish and the 15-second poller both drain,
# and uploads are backdated so both see a file as settled: unserialised,
# both filed it, minting two track UUIDs, and the loser reported a spurious
# failure. Drains are quick, so one lock for every workspace is enough.
_drain_lock = threading.Lock()


def drain(space: workspace.Workspace) -> Result:
    """File everything that has stopped changing and is not filed yet.

    The inbox, plus anything loose at the top of the library - those have
    never been through the filer either, and this is the only thing that
    looks at them.
    """
    with _drain_lock:
        return _drain(space)


def _drain(space: workspace.Workspace) -> Result:
    result = Result()
    for path in waiting(space) + loose_in_library(space):
        if not settled(path):
            result.waiting += 1
            continue
        remembered = _unfilable.get(path)
        if remembered and remembered[0] == _size_of(path):
            result.failures.append(remembered[1])
            continue
        try:
            filed = filer.file_track(space, path)
        except Exception as exc:
            message = f"{path.name}: {type(exc).__name__}: {exc}"
            _unfilable[path] = (_size_of(path), message)
            result.failures.append(message)
            log.exception("could not file %s for %s - leaving it alone until "
                          "it changes", path.name, space.username)
            continue
        _unfilable.pop(path, None)
        _arrived(filed.path.parent)
        if not filed.identified:
            result.failures.append(
                f"{filed.path.name}: filed, but its identity tags could not "
                f"be written, so stars and play counts cannot follow it")
        result.filed.append(filed.path)

    _clear_residue(space)
    _prune(space.inbox_dir)
    if result.filed:
        log.info("filed %d file(s) for %s",
                 len(result.filed), space.username)
    return result


# Audio in a format the filer cannot tag, so cannot file. Not on this list
# and not audio at all, a file is residue (see `_clear_residue`).
_CANNOT_FILE = {".aac", ".aif", ".alac", ".dff", ".dsf", ".m4b", ".mka",
                ".mpc", ".tta", ".wma"}


def overlooked(space: workspace.Workspace) -> list[str]:
    """Files in the inbox that will never be filed, and why.

    `waiting()` passes over them in silence, so they used to sit there for
    ever, counted nowhere: a format the filer cannot tag, anything under a
    hidden folder (which the poller skips on purpose - `.incomplete` and a
    `.Trash` folder live there), and a file dated in the future, which never
    looks settled.
    """
    root = space.inbox_dir
    if not root.is_dir():
        return []
    now = time.time()
    found = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] == ".incomplete" or path.name.startswith("."):
            continue
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        audio = uuidtags.is_audio(path)
        if not (audio or suffix in _CANNOT_FILE):
            continue
        if any(part.startswith(".") for part in relative.parts[:-1]):
            found.append(f"{relative}: in a hidden folder, which is never filed")
        elif not audio:
            found.append(f"{relative}: a format this cannot tag, so cannot file")
        else:
            try:
                ahead = path.stat().st_mtime - now
            except OSError:
                continue
            if ahead > 60:
                found.append(f"{relative}: dated in the future, so it never "
                             "looks finished arriving")
    return found


# The last pass's account of each workspace, for Health: what is waiting,
# what could not be filed and why, and what will never be. Keyed by the
# workspace's directory, since one person can have one per library.
_status: dict[str, dict[str, Any]] = {}
_status_lock = threading.Lock()


def _record(space: workspace.Workspace, result: Result) -> None:
    try:
        missed = overlooked(space)
    except OSError:
        missed = []
    with _status_lock:
        _status[space.key] = {
            "username": space.username,
            "library": space.library_name,
            "waiting": result.waiting,
            "failures": list(result.failures),
            "overlooked": missed,
            "broken": result.broken,
            "at": time.time(),
        }


def status_for(username: str) -> list[dict[str, Any]]:
    """This person's inboxes as the poller last saw them, and nobody else's."""
    with _status_lock:
        return [dict(entry) for entry in _status.values()
                if entry["username"] == username]


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
            if space.library_path not in _unmounted:
                _unmounted.add(space.library_path)
                log.warning("%s's library at %s is not mounted; leaving the "
                            "inbox alone", space.username, space.library_path)
            continue
        _unmounted.discard(space.library_path)

        # Make the inbox if it is not there. Nothing else does: `prepare()`
        # otherwise runs only when a download is queued or an album is
        # matched - so a workspace that has done neither since this code
        # shipped has nowhere to drop a file, and
        # `waiting()` reports that as an empty inbox rather than a missing
        # one. The README tells people to drop music into a directory the
        # application had never made for them.
        #
        # Idempotent, and this loop runs every POLL_SECONDS anyway.
        try:
            space.prepare()
        except Exception as exc:
            # A marker naming somebody else, a read-only mount. Neither is
            # this loop's to resolve, and neither should stop the others.
            log.warning("could not prepare %s's workspace: %s",
                        space.username, exc)
            continue

        # One workspace's failure is its own. A directory that could not be
        # listed used to raise out of here, and nobody's inbox was filed.
        try:
            result = drain(space)
        except Exception as exc:
            log.exception("could not drain %s's inbox", space.username)
            result = Result(broken=f"{type(exc).__name__}: {exc}"[:300])
        _record(space, result)
        if result.changed or result.failures or result.broken:
            results[space.username] = result
    return results
