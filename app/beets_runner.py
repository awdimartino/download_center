"""Hands finished downloads to beets for tagging and filing.

Everything this application writes is deliberately provisional. Tags are seeded
from Spotify so that beets has something accurate to match against, but beets
is what decides the canonical release, writes MusicBrainz identifiers, and
moves the file into the library tree. Matching a whole album at once - on track
count, ordering, durations and artist together - is far more reliable than the
per-track identifier lookups this could attempt on its own.

Albums and singles are imported separately because beets treats them
differently: an album import matches against a release, a singleton import
matches individual recordings. Sending loose tracks through album matching is
what makes an import stop and ask.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import navidrome, stamp, workspace
from .config import CONFIG_DIR, settings

log = logging.getLogger("download_center.beets")

# Serialises every beets invocation. Two processes writing one library.db
# and moving files into the same tree is how things get lost.
_import_lock = threading.Lock()

# Kept for the one-off migration of the single-user layout; every other
# reference goes through a workspace.
LEGACY_BEETS_DIR = CONFIG_DIR / "beets"

# Long enough for art fetching and MusicBrainz lookups on a slow connection,
# short enough that a wedged import cannot block the queue forever.
TIMEOUT = 900

DEFAULT_CONFIG = """\
# Written by Download Center on first run. Edit freely - it is never
# overwritten, and the container reads it on every import.

directory: __DIRECTORY__
library: __LIBRARY__

ui:
  # Beets colourises --pretend output even when it is redirected to a file,
  # embedding escape codes inside the paths it prints. Anything parsing that
  # output then matches nothing, silently.
  color: no

import:
  # Files are moved out of the staging area into `directory` above.
  move: yes
  write: yes
  # Unattended: never prompt. Anything beets is not confident about is left
  # in staging rather than guessed at, so you can look at it later.
  quiet: yes
  quiet_fallback: skip
  # Downloading a track from an album already held is the ordinary case, not
  # an error. `skip` would leave every such track sitting in staging for a
  # human; `merge` files it alongside its siblings, where it also inherits
  # the album UUID they already share instead of founding a second copy.
  duplicate_action: merge
  log: __LOG__

# Left at the defaults deliberately. Loosening strong_rec_thresh, or telling
# beets to ignore the missing_tracks penalty, lets a single downloaded song
# match a whole release confidently - and it is then filed as a one-track
# album under that release's name. Do that twice for the same record against
# two different releases and the album exists twice, permanently.

paths:
  # No year in the album directory, and no %aunique{}: both would file two
  # releases of one record into separate folders. One directory is one album
  # is what lets a later arrival inherit the album UUID its siblings already
  # share instead of founding a second copy. Navidrome sorts on the year tag,
  # not the folder name, so nothing is lost by leaving it out.
  # The %if{} fallbacks matter - an empty field collapses the path component
  # and drops the file loose into the artist folder. They must be quoted:
  # YAML forbids a plain scalar starting with '%'.
  default: '%if{$albumartist,$albumartist,%if{$artist,$artist,Unknown Artist}}/%if{$album,$album,Unknown Album}/$track - $title'
  singleton: 'Non-Album/$artist/$title'
  comp: 'Compilations/%if{$album,$album,Unknown Album}/$track - $title'

# musicbrainz must be listed explicitly: since beets 2.x it is a plugin, and
# naming any plugins here replaces the default list rather than adding to it.
# Omitting it disables album matching entirely, and every import silently
# skips with "Evaluating 0 candidates".
plugins: musicbrainz fetchart embedart

fetchart:
  auto: yes

embedart:
  auto: yes

# Uncomment to verify audio by acoustic fingerprint rather than by tags.
# Requires the chromaprint tools, which are already in the image.
# plugins: musicbrainz fetchart embedart chroma
"""


def ensure_config(space: workspace.Workspace) -> Path:
    """Create this person's beets config on first use; never overwrite it.

    The destination is filled in from their Navidrome library, so adding a
    user is nothing more than them signing in once.
    """
    space.prepare()
    if not space.beets_config.exists():
        # Substituted rather than formatted: the template is full of beets
        # path syntax like %if{$albumartist,...}, which str.format reads as
        # replacement fields and rejects.
        filled = DEFAULT_CONFIG
        for placeholder, value in (
            ("__DIRECTORY__", space.library_path),
            ("__LIBRARY__", space.beets_library),
            ("__LOG__", space.beets_dir / "import.log"),
        ):
            filled = filled.replace(placeholder, str(value))
        space.beets_config.write_text(filled, encoding="utf-8")
        log.info("wrote a beets config for %s at %s",
                 space.username, space.beets_config)
    return space.beets_config


def _run(space: workspace.Workspace, path: Path,
         singleton: bool) -> tuple[bool, str]:
    # Invoked through the interpreter rather than the `beet` script, which
    # is only on PATH when beets is installed system-wide.
    command = [sys.executable, "-m", "beets", "import",
               "-qs" if singleton else "-q", str(path)]
    environment = {**os.environ, "BEETSDIR": str(space.beets_dir)}
    try:
        result = subprocess.run(
            command, env=environment, capture_output=True, text=True,
            timeout=TIMEOUT, check=False,
        )
    except FileNotFoundError:
        return False, "beets is not available in this environment"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {TIMEOUT}s"

    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return False, output[-400:] or f"exit code {result.returncode}"
    return True, output[-400:]


def library_root(space: workspace.Workspace) -> Path:
    """Where beets files this person's music."""
    return space.library_path


def _library_size(space: workspace.Workspace) -> int:
    """How many items beets has indexed for this person.

    The authority on whether an import actually filed anything. Returns -1
    when the database cannot be read, which never compares greater than a
    previous count, so an unreadable database reads as "nothing imported"
    rather than as a spurious success.
    """
    library_db = space.beets_library
    if not library_db.exists():
        return 0
    try:
        connection = sqlite3.connect(f"file:{library_db}?mode=ro", uri=True,
                                     timeout=10)
        with connection:
            return connection.execute("select count(*) from items").fetchone()[0]
    except sqlite3.Error as exc:
        log.warning("could not count the beets library: %s", exc)
        return -1


def filed_since(space: workspace.Workspace, moment: float) -> list[Path]:
    """Paths beets added to the library after `moment`.

    Beets records where every file ended up, so asking it beats guessing from
    import output or re-walking the library. Its own database is the only
    place that knows, and this process owns it.
    """
    library_db = space.beets_library
    if not library_db.exists():
        return []
    try:
        connection = sqlite3.connect(f"file:{library_db}?mode=ro", uri=True,
                                     timeout=10)
        with connection:
            rows = connection.execute(
                "select path from items where added >= ?", (moment,)).fetchall()
    except sqlite3.Error as exc:
        log.warning("could not read the beets library: %s", exc)
        return []

    # Paths are stored as bytes, since a filesystem path is not necessarily
    # valid text in any encoding - and, since beets 2.x, relative to the
    # library directory. Resolving them is not optional: a relative path
    # silently resolves against the working directory instead, matches
    # nothing, and stamping quietly does nothing at all.
    root = space.library_path
    paths = []
    for row in rows:
        path = Path(os.fsdecode(row[0]))
        paths.append(path if path.is_absolute() else root / path)
    return paths


def import_paths(space: workspace.Workspace,
                 published: list[Path]) -> dict[str, Any]:
    """Import freshly published paths, returning a summary for the UI.

    Only what this job produced is imported, never the whole staging tree, so
    a concurrent job or another tool writing there is left alone.
    """
    if not settings.beets_enabled:
        return {"ran": False, "reason": "disabled"}

    # Two beets processes moving files into one tree is a way to lose things.
    # Held across every workspace rather than per user: they have separate
    # databases but the machine has one disk, and a sweep plus a finishing
    # job is the collision worth avoiding.
    #
    # Acquired without blocking. Waiting here meant a threadpool worker sat
    # on this lock for as long as the running import took - up to 900s per
    # path - and enough of those starve every other `to_thread` call in the
    # application. Refusing is honest and costs nothing: an import is a sweep
    # over whatever is waiting, so the one already running will pick up this
    # caller's paths too if they have settled, and the timer catches the rest.
    if not _import_lock.acquire(blocking=False):
        log.info("an import is already running; not starting another")
        return {"ran": False, "reason": "an import is already running",
                "busy": True}
    try:
        return _import_paths(space, published)
    finally:
        _import_lock.release()


def _import_paths(space: workspace.Workspace,
                  published: list[Path]) -> dict[str, Any]:
    ensure_config(space)
    imported, skipped, failed = 0, 0, []
    # Recorded before the first import so nothing filed during the run is
    # missed, at the cost of occasionally re-checking a file already stamped.
    started = time.time()

    for path in published:
        if not path.exists():
            continue
        # Singles are files; album imports are whole directories.
        singleton = path.is_file()
        before = _library_size(space)
        ok, output = _run(space, path, singleton)
        if not ok:
            failed.append(f"{path.name}: {output.splitlines()[-1] if output else 'failed'}")
            log.warning("beets import failed for %s: %s", path.name, output)
            continue
        # Asked of beets' own database rather than of its console output.
        # This used to test `"Skipping" in output`, which is a human-readable
        # message that changes between versions and matches any path with the
        # word in it. Stamping and the Navidrome scan are both gated on the
        # answer, so getting it wrong silently switched off identity tagging
        # for the import.
        if _library_size(space) > before:
            imported += 1
            log.info("beets imported %s", path.name)
        else:
            skipped += 1
            log.info("beets skipped %s (no confident match)", path.name)

    _prune_empty(space)

    # Identity is assigned here rather than at download time, because the
    # album UUID cannot be chosen until the file is in its final directory
    # and its siblings are visible. See app/stamp.py.
    stamped = (stamp.stamp(filed_since(space, started))
               if imported else stamp.Result())
    if stamped.changed:
        log.info("stamped %d new track UUID(s), %d album UUID(s)",
                 stamped.tracks_written, stamped.albums_written)

    # Freshly imported files are new to Navidrome, so an ordinary scan finds
    # them; the tags were written before it ever looked. Only re-tagging
    # existing files needs a full scan, since stamping preserves mtime.
    if imported:
        navidrome.notify()

    return {
        "ran": True,
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "stamped": stamped.tracks_written,
        "stamp_failures": stamped.failures,
    }


def settled(path: Path, quiet_seconds: int | None = None) -> bool:
    """Whether a path has stopped changing and is safe to import.

    A directory still being written to imports as a partial album, which is
    exactly the mistake this pipeline exists to avoid. Nothing here can know
    whether a downloader is mid-run, so it waits for stillness instead.
    """
    if quiet_seconds is None:
        quiet_seconds = settings.staging_quiet_seconds
    cutoff = time.time() - quiet_seconds
    newest = path.stat().st_mtime
    if path.is_dir():
        for child in path.rglob("*"):
            newest = max(newest, child.stat().st_mtime)
    return newest <= cutoff


def waiting_in(space: workspace.Workspace) -> list[Path]:
    """What is sitting in one person's staging area, ready to import."""
    candidates: list[Path] = []
    for parent, want_dirs in ((space.albums_dir, True),
                              (space.singles_dir, False)):
        if not parent.is_dir():
            continue
        for entry in parent.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_dir() != want_dirs:
                continue
            if not settled(entry):
                log.debug("%s is still changing, leaving it", entry.name)
                continue
            candidates.append(entry)
    return candidates


def sweep_staging() -> dict[str, Any]:
    """Import anything sitting in staging that no download job put there.

    Files arrive by other routes - a manual drop, a job that finished while
    beets was busy, something copied in from elsewhere - and without this they
    would sit in staging indefinitely. Beets moves out what it can match, so
    whatever remains afterwards is by definition something that needs a human.

    Runs on a timer with nobody signed in, which is exactly why staging is
    split by person: the folder is the only remaining record of whose files
    these are and where they should end up.
    """
    if not settings.beets_enabled:
        return {"ran": False, "reason": "disabled"}

    results: dict[str, Any] = {}
    for space in workspace.existing():
        candidates = waiting_in(space)
        if not candidates:
            continue
        log.info("sweeping %d item(s) from %s's staging",
                 len(candidates), space.username)
        results[space.username] = import_paths(space, candidates)

    if not results:
        return {"ran": False, "reason": "nothing waiting"}
    return {"ran": True, "by_user": results}


def _prune_empty(space: workspace.Workspace) -> None:
    """Remove album directories beets emptied when it moved the files out.

    Anything still holding files was skipped rather than imported, and is
    left alone so it stays visible.
    """
    for parent in (space.albums_dir, space.singles_dir):
        if not parent.is_dir():
            continue
        for entry in parent.iterdir():
            if entry.is_dir() and not any(entry.rglob("*")):
                entry.rmdir()
