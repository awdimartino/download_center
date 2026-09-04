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
import time
from pathlib import Path
from typing import Any

from . import stamp
from .config import CONFIG_DIR, settings

log = logging.getLogger("download_center.beets")

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
  duplicate_action: skip
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
    except sqlite3.Error as exc:
        log.warning("could not read the beets library: %s", exc)
        return []
    with connection:
        rows = connection.execute(
            "select path from items where added >= ?", (moment,)).fetchall()
    # beets stores paths as bytes, since a filesystem path is not necessarily
    # valid text in any encoding.
    return [Path(os.fsdecode(row[0])) for row in rows]


def import_paths(published: list[Path]) -> dict[str, Any]:
    """Import freshly published paths, returning a summary for the UI.

    Only what this job produced is imported, never the whole staging tree, so
    a concurrent job or another tool writing there is left alone.
    """
    if not settings.beets_enabled:
        return {"ran": False, "reason": "disabled"}

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

    return {
        "ran": True,
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "stamped": stamped.tracks_written,
        "stamp_failures": stamped.failures,
    }


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
