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

from . import ledger, navidrome, stamp, staging, uuidtags, workspace
from .config import CONFIG_DIR, settings

log = logging.getLogger("download_center.beets")

# Serialises every beets invocation. Two processes writing one library.db
# and moving files into the same tree is how things get lost.
_import_lock = threading.Lock()

# What beets looked at and would not file. Kept in state.db rather than in
# memory, because the sweep has to know it across a restart: it used to retry
# every stuck item every run, so a backlog of things that will never match
# meant the sweep ran continuously - and one beets process holds the import
# lock, so every manual import and every candidate lookup was refused with
# "an import is already running". Staging became unusable in proportion to
# how much was stuck in it.
#
# A refusal is remembered against the file's mtime. Change the file and it is
# a different question, asked again.

# How long a refusal stands before the sweep tries once more. MusicBrainz
# gains releases; an album nobody had entered in March may be there in June.
RETRY_REFUSED_AFTER = 7 * 24 * 3600

# What the sweep is doing right now, for the panel. A sweep with a hundred
# items to walk is otherwise indistinguishable from a wedged one.
_progress: dict[str, Any] = {}
_progress_lock = threading.Lock()


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
    """Why beets last refused this path, if it did and it still stands."""
    try:
        row = ledger.connection().execute(
            "SELECT reason, at, mtime FROM import_refusal WHERE path = ?",
            (_key(path),)).fetchone()
    except sqlite3.Error:
        log.debug("could not read refusals", exc_info=True)
        return None
    if row is None:
        return None
    return {"reason": row[0], "at": row[1], "mtime": row[2]}


def _remember_refusal(path: Path, reason: str) -> None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    try:
        with ledger.connection() as connection:
            connection.execute(
                "INSERT INTO import_refusal (path, reason, at, mtime)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(path) DO UPDATE SET"
                " reason = excluded.reason, at = excluded.at,"
                " mtime = excluded.mtime",
                (_key(path), reason, time.time(), mtime))
    except sqlite3.Error:
        log.warning("could not record a refusal for %s", path.name,
                    exc_info=True)


def _forget_refusals(paths: list[Path]) -> None:
    """Drop notes for paths that were filed, or have otherwise gone."""
    try:
        with ledger.connection() as connection:
            for path in paths:
                connection.execute("DELETE FROM import_refusal WHERE path = ?",
                                   (_key(path),))
            # Staging names get reused. A note left behind would attach
            # itself to the next thing to arrive under the same name.
            for (recorded,) in connection.execute(
                    "SELECT path FROM import_refusal").fetchall():
                if not Path(recorded).exists():
                    connection.execute(
                        "DELETE FROM import_refusal WHERE path = ?", (recorded,))
    except sqlite3.Error:
        log.warning("could not clear refusals", exc_info=True)


def worth_trying(path: Path) -> bool:
    """Whether the *unattended* sweep should offer this to beets again.

    Only the sweep asks. Anything a person presses - Try importing now,
    Import as-is, choosing a release - goes ahead regardless: they can see
    the item and they are allowed to disagree.
    """
    record = refusal(path)
    if record is None:
        return True
    try:
        if path.stat().st_mtime > record["mtime"]:
            return True
    except OSError:
        return True
    return (time.time() - record["at"]) > RETRY_REFUSED_AFTER


def progress() -> dict[str, Any]:
    """What the sweep is working on, if anything."""
    with _progress_lock:
        return dict(_progress)


def _set_progress(**fields: Any) -> None:
    with _progress_lock:
        if fields:
            _progress.update(fields)
        else:
            _progress.clear()


# Kept for the one-off migration of the single-user layout; every other
# reference goes through a workspace.
LEGACY_BEETS_DIR = CONFIG_DIR / "beets"

# Long enough for art fetching and MusicBrainz lookups on a slow connection,
# short enough that a wedged import cannot block the queue forever.
TIMEOUT = 900

# How long a caller waits for the import lock before reporting it busy. Long
# enough to outlast one ordinary item, short enough that a threadpool worker
# blocked here is never a problem.
LOCK_WAIT = 45

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

# strong_rec_thresh is left at its default, and must stay there. Loosening it
# moves the distance gate itself, which really would let a single downloaded
# song match a whole release confidently and be filed as a one-track album
# under that release's name - twice over, against two releases, and the album
# exists twice permanently.
#
# max_rec is a different lever and is safe to lift. It caps the
# *recommendation* and does nothing to the distance, so the gate still holds.
# This used to be left alone out of the fear above, which measurement does not
# support - a fragment's distance rises steeply with what is missing:
#
#     1 of 10 tracks   distance 0.5031   rec none     not filed
#     5 of 10 tracks   distance 0.2195   rec medium   not filed
#     9 of 10 tracks   distance 0.0011   rec strong   filed
#
# So the cap was not protecting against one-track albums - distance already
# does that, by an order of magnitude. What it did instead was throw away
# albums missing a single track, which is most of what used to pile up in
# staging: correctly identified, then discarded for being one track short.
#
# unmatched_tracks stays capped. That penalty is what stops a folder holding
# extra, unrelated tracks from being filed as an album, and staging is full of
# loose tracks that would hit exactly that case.
match:
  max_rec:
    missing_tracks: strong

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
#
# chroma identifies a recording by what it sounds like rather than by what its
# tags claim, which is the only thing that helps a file whose tags are wrong or
# absent - and most hand-dropped rips are. It needs fpcalc, from
# libchromaprint-tools in the image, *and* pyacoustid, which it reaches fpcalc
# through; the dependency was missing for a long time while a comment here
# claimed the feature was ready to switch on.
plugins: musicbrainz fetchart embedart chroma

fetchart:
  auto: yes

embedart:
  auto: yes

chroma:
  auto: yes
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
         as_is: bool = False, release_id: str | None = None) -> tuple[bool, str]:
    # A release the library already agreed on is applied rather than searched
    # for. Beets is still asked - `--apply` answers its own choose_match with
    # this release - so the file is tagged from MusicBrainz exactly as a
    # matched import would be; what is skipped is only the confidence test,
    # which a fragment cannot pass and does not need to. See held_release.
    if release_id:
        command = [sys.executable, "-m", "app.beets_match",
                   "--apply", release_id, str(path)]
        environment = {**os.environ, "BEETSDIR": str(space.beets_dir)}
        try:
            result = subprocess.run(
                command, env=environment, capture_output=True, text=True,
                timeout=TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired:
            return False, f"timed out after {TIMEOUT}s"
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output[-400:]

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


# Signatures in beets' own output for the ways an import can file nothing.
# Ordered: the first match wins, so the specific causes are tested before the
# catch-all.
_NETWORK_SIGNS = (
    "temporary failure in name resolution", "failed to resolve",
    "network is unreachable", "connectionerror", "max retries exceeded",
    "connection refused", "timed out",
)
_DUPLICATE_SIGNS = ("duplicate-keep", "duplicate-replace", "skipping duplicate")
_NO_CANDIDATE_SIGNS = ("no matching release found", "no match found")


def held_release(space: workspace.Workspace, path: Path) -> str | None:
    """The MusicBrainz release this staged folder's album is already filed as.

    Beets matches a staging folder against MusicBrainz in isolation: it has
    no idea the rest of the record is already in the library. So downloading
    the four tracks of a twelve track album you own eight of asks it to match
    four tracks against a twelve track release, which scores badly and is
    refused - and the fragment waits in staging next to its own siblings.

    Asked here instead, of the library, which does know. When the answer is
    yes the release is not in question any more and does not need matching
    again; `app.beets_match --apply` files the fragment as that exact
    release, which is also what keeps every track of one record on one
    release rather than scattered across whichever edition matched best that
    day.

    Returns None when the album is unknown, or known but without a release
    id to reuse - both mean "match this normally". Being wrong in that
    direction costs a refusal somebody can act on; being wrong the other way
    writes a release id onto an album it does not belong to.
    """
    if not path.is_dir():
        return None
    # Artist *and* title. On the title alone this found an unrelated one-track
    # single called "sunburn" by almost monday for a staged thirteen-track
    # "Fuel - Sunburn", and would have filed the album as that single - which
    # is the same split-release damage this exists to prevent, only automated.
    described = {staging._album_of(p) for p in path.rglob("*")
                 if p.is_file() and p.suffix.lower() in staging.AUDIO_SUFFIXES}
    described = {d for d in described if d}
    # Two records in one folder is not a fragment of either, and picking one
    # of them would file the other under the wrong release.
    if len(described) != 1:
        return None
    artist, album = described.pop()

    library_db = space.beets_library
    if not library_db.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{library_db}?mode=ro", uri=True,
                                     timeout=5)
        try:
            rows = connection.execute(
                "SELECT mb_albumid, COUNT(*) FROM albums"
                " WHERE lower(album) = lower(?)"
                "   AND lower(albumartist) = lower(?) AND mb_albumid != ''"
                " GROUP BY mb_albumid ORDER BY COUNT(*) DESC",
                (album, artist)).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        log.debug("could not ask the library about %s", album, exc_info=True)
        return None

    # Exactly one release, or none. Several means the library itself already
    # disagrees about which release this record is, and adding a fragment to
    # whichever is commonest would entrench a split rather than resolve it.
    if len(rows) != 1:
        return None
    return rows[0][0]


def _why_nothing_filed(output: str, as_is: bool) -> str:
    """Why beets filed nothing, distinguished rather than guessed at.

    Four very different things used to arrive as one sentence, "beets found
    no confident match", and each of them sends you somewhere else: a broken
    resolver is not a tagging problem, and a duplicate is not a failure at
    all. A whole staging backlog was mis-read as unmatchable music when the
    container simply had no DNS, so the wrong reason is worse than a vague
    one - it is a wrong instruction about what to do next.
    """
    lowered = output.lower()
    if any(sign in lowered for sign in _NETWORK_SIGNS):
        return ("could not reach MusicBrainz - this is a network problem, "
                "not a tagging one")
    if any(sign in lowered for sign in _DUPLICATE_SIGNS):
        return "already in the library; beets kept the copy it had"
    if as_is:
        # An as-is import does no matching at all, so "no confident match"
        # was never a possible answer for it.
        return "nothing was filed, and beets said why in the log"
    if any(sign in lowered for sign in _NO_CANDIDATE_SIGNS):
        return "MusicBrainz has no release matching this"
    return "beets found no confident match"


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
    return _import_paths(space, published, as_is)


def _import_paths(space: workspace.Workspace, published: list[Path],
                  as_is: bool = False) -> dict[str, Any]:
    ensure_config(space)
    imported, skipped, failed = 0, 0, []
    filed: list[Path] = []
    # Recorded before the first import so nothing filed during the run is
    # missed, at the cost of occasionally re-checking a file already stamped.
    started = time.time()

    for index, path in enumerate(published, start=1):
        if not path.exists():
            continue
        # Said out loud: a sweep with a hundred items to walk looks exactly
        # like a wedged one from the panel.
        _set_progress(running=True, name=path.name, done=index - 1,
                      total=len(published), user=space.username)

        # One item, one turn with the lock.
        #
        # Two beets processes moving files into one tree is a way to lose
        # things, so only one runs at a time - across every workspace,
        # because they have separate databases but the machine has one disk.
        # What changed is the *granularity*: the lock used to be held for a
        # whole run, which was defensible when a sweep was three albums and
        # became absurd once one could walk a hundred stuck items for an
        # hour. Everything a person pressed in that hour answered "an import
        # is already running". Now the wait is one item long.
        if not _import_lock.acquire(timeout=LOCK_WAIT):
            if not (imported or skipped or failed):
                _set_progress()
                log.info("an import is already running; not starting another")
                return {"ran": False, "reason": "an import is already running",
                        "busy": True}
            log.info("stopping this run at %d of %d: something else wants beets",
                     index, len(published))
            break
        try:
            singleton = _singleton_mode(path, as_is)
            # Only for a real match. An as-is import is someone saying "file
            # it with what it has", and quietly tagging it from MusicBrainz
            # instead would be answering a different question.
            release = None if as_is else held_release(space, path)
            if release:
                log.info("%s belongs to a release the library already holds "
                         "(%s); filing it as that rather than matching it "
                         "alone", path.name, release[:8])
            before = _library_size(space)
            ok, output = _run(space, path, singleton, as_is, release)
            if not ok:
                detail = output.splitlines()[-1] if output else "failed"
                failed.append(f"{path.name}: {detail}")
                _remember_refusal(path, f"beets could not import this: {detail}")
                log.warning("beets import failed for %s: %s", path.name, output)
                continue
            # Asked of beets' own database rather than of its console output.
            # This used to test `"Skipping" in output`, which is a
            # human-readable message that changes between versions and matches
            # any path with the word in it. Stamping and the Navidrome scan are
            # both gated on the answer, so getting it wrong silently switched
            # off identity tagging for the import.
            if _library_size(space) > before:
                imported += 1
                filed.append(path)
                log.info("beets imported %s", path.name)
            else:
                skipped += 1
                reason = _why_nothing_filed(output, as_is)
                _remember_refusal(path, reason)
                log.info("beets skipped %s (%s)", path.name, reason)
        finally:
            _import_lock.release()

    _set_progress()
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


def swept_on(day: str) -> bool:
    """Whether the sweep has already run for a given local day."""
    try:
        row = ledger.connection().execute(
            "SELECT 1 FROM sweep_run WHERE day = ?", (day,)).fetchone()
    except sqlite3.Error:
        log.debug("could not read sweep history", exc_info=True)
        return False
    return row is not None


def record_sweep(day: str, summary: dict[str, Any]) -> None:
    try:
        with ledger.connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO sweep_run (day, ran_at, summary)"
                " VALUES (?, ?, ?)",
                (day, time.time(), json.dumps(summary)[:2000]))
    except sqlite3.Error:
        log.warning("could not record the sweep for %s", day, exc_info=True)


def sweep_staging() -> dict[str, Any]:
    """Import anything sitting in staging that no download job put there.

    Files arrive by other routes - a manual drop, a job that finished while
    beets was busy, something copied in from elsewhere - and without this they
    would sit in staging indefinitely.

    Only what is worth asking about. Beets is deterministic: an item it
    refused an hour ago, unchanged, gets refused again, and the sweep used to
    walk the whole backlog every time and hold the import lock for as long as
    that took. Everything a person pressed in the meantime came back "an
    import is already running". So a refusal stands until the file changes or
    a week passes; anything a person asks for ignores this entirely.

    Runs on a timer with nobody signed in, which is exactly why staging is
    split by person: the folder is the only remaining record of whose files
    these are and where they should end up.
    """
    if not settings.beets_enabled:
        return {"ran": False, "reason": "disabled"}

    results: dict[str, Any] = {}
    skipped_known = 0
    for space in workspace.existing():
        # One folder per album before anything is handed over. Files arrive
        # by routes that know nothing about each other - a job, a hand drop,
        # a copy from somewhere else - and what beets is asked about should
        # be decided by the tags rather than by which of those it came from.
        staging.regroup(space)
        waiting = waiting_in(space)
        candidates = [p for p in waiting if worth_trying(p)]
        skipped_known += len(waiting) - len(candidates)
        if not candidates:
            continue
        log.info("sweeping %d of %d item(s) from %s's staging",
                 len(candidates), len(waiting), space.username)
        results[space.username] = import_paths(space, candidates)

    if not results:
        return {"ran": False, "reason": "nothing new waiting",
                "already_refused": skipped_known}
    return {"ran": True, "by_user": results, "already_refused": skipped_known}


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
