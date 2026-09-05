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

from . import navidrome, stamp
from .config import CONFIG_DIR, settings

log = logging.getLogger("download_center.beets")

# Serialises every beets invocation. Two processes writing one library.db
# and moving files into the same tree is how things get lost.
_import_lock = threading.Lock()

BEETS_DIR = CONFIG_DIR / "beets"
CONFIG_PATH = BEETS_DIR / "config.yaml"

# Long enough for art fetching and MusicBrainz lookups on a slow connection,
# short enough that a wedged import cannot block the queue forever.
TIMEOUT = 900

DEFAULT_CONFIG = """\
# Written by Download Center on first run. Edit freely - it is never
# overwritten, and the container reads it on every import.

directory: /music
library: /config/beets/library.db

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
  log: /config/beets/import.log

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


def ensure_config() -> Path:
    """Create the beets config on first run; never overwrite an edited one."""
    BEETS_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG, encoding="utf-8")
        log.info("wrote a default beets config to %s", CONFIG_PATH)
    return CONFIG_PATH


def _run(path: Path, singleton: bool) -> tuple[bool, str]:
    # Invoked through the interpreter rather than the `beet` script, which
    # is only on PATH when beets is installed system-wide.
    command = [sys.executable, "-m", "beets", "import",
               "-qs" if singleton else "-q", str(path)]
    environment = {**os.environ, "BEETSDIR": str(BEETS_DIR)}
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


def library_root() -> Path:
    """The `directory` beets files into, from its own config."""
    try:
        import yaml
        raw = yaml.safe_load(ensure_config().read_text(encoding="utf-8")) or {}
        configured = raw.get("directory")
    except Exception:
        configured = None
    return Path(configured) if configured else settings.music_dir


def filed_since(moment: float) -> list[Path]:
    """Paths beets added to the library after `moment`.

    Beets records where every file ended up, so asking it beats guessing from
    import output or re-walking the library. Its own database is the only
    place that knows, and this process owns it.
    """
    library_db = BEETS_DIR / "library.db"
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
    root = library_root()
    paths = []
    for row in rows:
        path = Path(os.fsdecode(row[0]))
        paths.append(path if path.is_absolute() else root / path)
    return paths


def import_paths(published: list[Path]) -> dict[str, Any]:
    """Import freshly published paths, returning a summary for the UI.

    Only what this job produced is imported, never the whole staging tree, so
    a concurrent job or another tool writing there is left alone.
    """
    if not settings.beets_enabled:
        return {"ran": False, "reason": "disabled"}

    # Two beets processes against one library.db, both moving files into the
    # same tree, is a way to lose things. The sweep and a finishing job can
    # both land here, so they queue instead.
    with _import_lock:
        return _import_paths(published)


def _import_paths(published: list[Path]) -> dict[str, Any]:
    ensure_config()
    imported, skipped, failed = 0, 0, []
    # Recorded before the first import so nothing filed during the run is
    # missed, at the cost of occasionally re-checking a file already stamped.
    started = time.time()

    for path in published:
        if not path.exists():
            continue
        # Singles are files; album imports are whole directories.
        singleton = path.is_file()
        ok, output = _run(path, singleton)
        if not ok:
            failed.append(f"{path.name}: {output.splitlines()[-1] if output else 'failed'}")
            log.warning("beets import failed for %s: %s", path.name, output)
        elif "Skipping" in output:
            skipped += 1
            log.info("beets skipped %s (no confident match)", path.name)
        else:
            imported += 1
            log.info("beets imported %s", path.name)

    _prune_empty()

    # Identity is assigned here rather than at download time, because the
    # album UUID cannot be chosen until the file is in its final directory
    # and its siblings are visible. See app/stamp.py.
    stamped = stamp.stamp(filed_since(started)) if imported else stamp.Result()
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


def sweep_staging() -> dict[str, Any]:
    """Import anything sitting in staging that no download job put there.

    Files arrive by other routes - a manual drop, a job that finished while
    beets was busy, something copied in from elsewhere - and without this they
    would sit in staging indefinitely. Beets moves out what it can match, so
    whatever remains afterwards is by definition something that needs a human.
    """
    if not settings.beets_enabled:
        return {"ran": False, "reason": "disabled"}

    candidates: list[Path] = []
    for parent, want_dirs in ((settings.albums_dir, True),
                              (settings.singles_dir, False)):
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

    if not candidates:
        return {"ran": False, "reason": "nothing waiting"}

    log.info("sweeping %d item(s) from staging", len(candidates))
    return import_paths(candidates)


def _prune_empty() -> None:
    """Remove album directories beets emptied when it moved the files out.

    Anything still holding files was skipped rather than imported, and is
    left alone so it stays visible.
    """
    for parent in (settings.albums_dir, settings.singles_dir):
        if not parent.is_dir():
            continue
        for entry in parent.iterdir():
            if entry.is_dir() and not any(entry.rglob("*")):
                entry.rmdir()
