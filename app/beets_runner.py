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

Beets is configured never to guess, so anything it is unsure of stays in
staging - which is correct, and was also a dead end: nothing in the
application could then file it. `import_paths(..., as_is=True)` is the way
out. It imports with `--noautotag`, so no matching happens at all and the
file is filed under the tags it already carries, which for a downloaded
track are the ones seeded from Spotify. Which paths are refused, and why, is
remembered here so the UI can say so rather than showing a folder that looks
untouched.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import navidrome, stamp, uuidtags, workspace
from .config import CONFIG_DIR, settings

log = logging.getLogger("download_center.beets")

# Serialises every beets invocation. Two processes writing one library.db
# and moving files into the same tree is how things get lost.
_import_lock = threading.Lock()

# path -> {"reason", "at"} for everything beets looked at and would not file.
# A refusal is otherwise invisible: the folder simply stays where it is,
# which looks identical to nothing having run. Guarded by its own lock
# because the sweep writes it from a worker thread while a request reads it.
#
# Deliberately in memory. It is a note about the last attempt, not a fact
# about the library, and the next sweep rebuilds it; persisting it would
# mean a schema migration for something that is allowed to be missing.
_refused: dict[str, dict[str, Any]] = {}
_refused_lock = threading.Lock()


def _key(path: Path) -> str:
    """One spelling of a path, so a note can be found again.

    Resolved, because the two sides reach the same file differently: the
    staging listing walks `iterdir()`, while an item imported by name comes
    through `Workspace.staged()`, which resolves. Left as raw strings, a
    refusal recorded by one would simply never be found by the other - and
    the row would show no reason, silently, which is the failure this whole
    note exists to prevent.
    """
    return str(path.resolve())


def refusal(path: Path) -> dict[str, Any] | None:
    """Why beets last refused this path, if it did and it is still there."""
    with _refused_lock:
        return _refused.get(_key(path))


def _remember_refusal(path: Path, reason: str) -> None:
    with _refused_lock:
        _refused[_key(path)] = {"reason": reason, "at": time.time()}


def _forget_refusals(paths: list[Path]) -> None:
    """Drop notes for paths that were filed, or have otherwise gone."""
    with _refused_lock:
        for path in paths:
            _refused.pop(_key(path), None)
        for key in [k for k in _refused if not Path(k).exists()]:
            del _refused[key]

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


def _run(space: workspace.Workspace, path: Path, singleton: bool,
         as_is: bool = False) -> tuple[bool, str]:
    # Invoked through the interpreter rather than the `beet` script, which
    # is only on PATH when beets is installed system-wide.
    flags = ["-q"]
    if singleton:
        flags.append("-s")
    if as_is:
        # --noautotag. Not `--quiet-fallback=asis`, which would still spend a
        # MusicBrainz lookup per file before giving the same answer: this
        # button exists because matching has already been tried and failed,
        # and a person is waiting on it.
        flags.append("-A")
    # Every flag before the path: optparse accepts them interspersed, but a
    # path that begins with a dash would then be read as one.
    command = [sys.executable, "-m", "beets", "import", *flags, str(path)]
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


def _singleton_mode(path: Path, as_is: bool) -> bool:
    """Whether to import this path as loose tracks rather than as an album.

    The answer differs by mode because the flag means two different things.
    Matching a fragment of a release against that release is what makes an
    import stop and ask, so a loose *file* is matched as a singleton. With
    `--noautotag` nothing is matched, and the flag only chooses a path
    template: a file carrying an album tag then belongs in
    `$albumartist/$album/`, the same folder its siblings will land in, so a
    later arrival joins it instead of founding a second copy. Only a file
    with no album to belong to goes to `Non-Album/`.
    """
    if not path.is_file():
        return False
    return not uuidtags.album_name(path) if as_is else True


def import_paths(space: workspace.Workspace, published: list[Path],
                 as_is: bool = False) -> dict[str, Any]:
    """Import freshly published paths, returning a summary for the UI.

    Only what this job produced is imported, never the whole staging tree, so
    a concurrent job or another tool writing there is left alone.

    With `as_is`, beets does no matching and files each path under the tags
    it already has. That is the escape hatch for what it refused, and it is
    never automatic: nothing calls this without someone asking for it.
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
        return _import_paths(space, published, as_is)
    finally:
        _import_lock.release()


def _import_paths(space: workspace.Workspace, published: list[Path],
                  as_is: bool = False) -> dict[str, Any]:
    ensure_config(space)
    imported, skipped, failed = 0, 0, []
    filed: list[Path] = []
    # Recorded before the first import so nothing filed during the run is
    # missed, at the cost of occasionally re-checking a file already stamped.
    started = time.time()

    for path in published:
        if not path.exists():
            continue
        singleton = _singleton_mode(path, as_is)
        before = _library_size(space)
        ok, output = _run(space, path, singleton, as_is)
        if not ok:
            detail = output.splitlines()[-1] if output else "failed"
            failed.append(f"{path.name}: {detail}")
            _remember_refusal(path, f"beets could not import this: {detail}")
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
            filed.append(path)
            log.info("beets imported %s", path.name)
        else:
            skipped += 1
            # An as-is import that files nothing is not "no confident match"
            # - there was no matching. Something else stopped it, and saying
            # the wrong reason is worse than saying little.
            _remember_refusal(path, "nothing was filed, and beets said why in the log"
                              if as_is else "beets found no confident match")
            log.info("beets skipped %s (%s)", path.name,
                     "as-is import filed nothing" if as_is
                     else "no confident match")

    # Only what was filed: a path that was refused again this run has just
    # had its note rewritten, and must keep it.
    _forget_refusals(filed)
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
        "as_is": as_is,
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


# --- choosing a match by hand ----------------------------------------------
# The other half of the escape hatch. Import as-is says "file it with what it
# has"; this says "it is *this* release, use that". Wanted where the seeded
# tags are wrong rather than merely unconfirmed.

# Beets gets a while: a candidate lookup is several MusicBrainz round trips
# and this runs on a Raspberry Pi. Shorter than the import timeout because
# nothing is being written and a person is watching a spinner.
MATCH_TIMEOUT = 180


def candidates(space: workspace.Workspace, path: Path) -> dict[str, Any]:
    """What beets would match this staged path against, in its own order."""
    ensure_config(space)
    command = [sys.executable, "-m", "app.beets_match", str(path)]
    environment = {**os.environ, "BEETSDIR": str(space.beets_dir)}
    try:
        result = subprocess.run(
            command, env=environment, capture_output=True, text=True,
            timeout=MATCH_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"looking for matches took longer than "
                         f"{MATCH_TIMEOUT}s", "candidates": []}
    except FileNotFoundError:
        return {"error": "beets is not available here", "candidates": []}

    # Scanned backwards for the answer rather than assuming it is the last
    # line. Beets talks on the way past - plugins announce missing API keys,
    # and a database migration prints a backup path per table - and it does
    # some of it *after* the command has run. Anchoring on either end of the
    # output is a silent failure waiting for the next beets release.
    answer = None
    for line in reversed((result.stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            answer = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if answer is None:
        log.warning("could not read the match output for %s: %s",
                    path.name, (result.stdout or result.stderr)[-300:])
        return {"error": "beets did not answer in a form this could read",
                "candidates": []}
    answer.setdefault("candidates", [])
    answer["name"] = path.name
    return answer


def import_chosen(space: workspace.Workspace, path: Path,
                  release_id: str) -> dict[str, Any]:
    """File a staged path as the release a person picked.

    Beets is asked rather than overruled: `app.beets_match --apply` answers
    its own choose_match with the release named here, which applies that
    match whatever the confidence numbers say. Loosening the thresholds
    instead was tried first and does not even work - measured against a real
    staged album, `--search-id` with `strong_rec_thresh` raised *and* the
    `max_rec` caps lifted still filed nothing, because quiet mode applies
    only on a strong recommendation and missing tracks cap it at medium.
    """
    if not settings.beets_enabled:
        return {"ran": False, "reason": "disabled"}
    if not _import_lock.acquire(blocking=False):
        return {"ran": False, "reason": "an import is already running",
                "busy": True}
    try:
        ensure_config(space)
        before = _library_size(space)
        started = time.time()
        command = [sys.executable, "-m", "app.beets_match",
                   "--apply", release_id, str(path)]
        try:
            result = subprocess.run(
                command, env={**os.environ, "BEETSDIR": str(space.beets_dir)},
                capture_output=True, text=True, timeout=TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            return {"ran": True, "imported": 0, "skipped": 0,
                    "failed": [f"{path.name}: timed out after {TIMEOUT}s"]}

        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            detail = output.splitlines()[-1] if output else "failed"
            _remember_refusal(path, f"beets could not import this: {detail}")
            log.warning("choosing a release failed for %s: %s", path.name, output)
            return {"ran": True, "imported": 0, "skipped": 0,
                    "failed": [f"{path.name}: {detail}"]}

        # Asked of the library, not of the subprocess: it reports what it
        # chose, and this reports what actually arrived.
        if _library_size(space) <= before:
            _remember_refusal(
                path, "the chosen release did not file it; beets said why in "
                      "the log")
            log.info("chosen release %s filed nothing for %s",
                     release_id, path.name)
            return {"ran": True, "imported": 0, "skipped": 1, "failed": [],
                    "chosen": release_id}

        _forget_refusals([path])
        _prune_empty(space)
        stamped = stamp.stamp(filed_since(space, started))
        navidrome.notify()
        log.info("filed %s as %s", path.name, release_id)
        return {"ran": True, "imported": 1, "skipped": 0, "failed": [],
                "chosen": release_id, "stamped": stamped.tracks_written,
                "stamp_failures": stamped.failures}
    finally:
        _import_lock.release()
