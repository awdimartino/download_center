"""ReplayGain, measured by rsgain one album folder at a time.

The health panel has counted tracks with no ReplayGain since it was written,
and the only remedy it could name was `beet replaygain` over SSH - which no
longer reaches most of the library, because beets stopped being the way music
comes in and its database does not know about anything the filer filed.

rsgain rather than a loudness pass through ffmpeg: it is the reference
ReplayGain 2.0 scanner, it writes the tag each container actually uses (TXXX
on MP3, the `com.apple.iTunes` freeform atom on M4A, Vorbis comments on FLAC),
and its easy mode treats one folder as one album - which is exactly what the
filer guarantees a folder is. Verified against copies of real files from this
library: both UUID tags survive on all three formats. The one thing it drops
is Vorbis comments that are present but empty, which beets writes as
placeholders and which carry nothing.

A whole folder is always measured, never only the tracks that lack a value.
Album gain is the loudness of the album as a whole, so measuring the three new
tracks of a twelve-track record would give them an album gain that disagrees
with the other nine.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import filer, inbox, library, navidrome, operations

log = logging.getLogger("navidrome_companion.replaygain")

# The operation's name, for starting it, reporting on it and stopping it.
NAME = "replaygain"

# Per album. A long classical box set on a Pi is minutes, not hours; anything
# past this is stuck, not slow.
TIMEOUT = 1800


class Unavailable(RuntimeError):
    """rsgain is not installed here."""


def available() -> bool:
    return shutil.which("rsgain") is not None


def measure(folder: Path) -> None:
    """Measure and tag every track in one album folder.

    Raises rather than returning a flag. A scanner that exits non-zero has
    usually tagged nothing, and "done" over an unchanged album is the silent
    failure this codebase keeps meeting.
    """
    if not available():
        raise Unavailable(
            "rsgain is not installed in this container, so nothing can be "
            "measured. It is in the image from this release on.")
    # One thread: this shares a Raspberry Pi with Navidrome, which is
    # streaming to somebody while it runs.
    result = subprocess.run(
        ["rsgain", "easy", "--multithread=1", "--quiet", str(folder)],
        capture_output=True, text=True, timeout=TIMEOUT, check=False)
    if result.returncode != 0:
        said = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"rsgain failed on {folder.name}: "
            f"{said[-1] if said else f'exit {result.returncode}'}")
    log.info("measured ReplayGain for %s", folder)


def measure_all(identity: navidrome.Identity,
                targets: list[tuple[int, str]]) -> dict[str, Any]:
    """Measure each (library_id, folder), reporting as it goes.

    Runs as the `replaygain` operation: it reports after every album and
    checks between albums whether it has been asked to stop. One album
    failing does not end the run - it is listed and the rest carry on.
    """
    owner = identity.username
    done, failed, skipped = 0, [], []
    for n, (library_id, folder) in enumerate(targets):
        if operations.stopping(NAME, owner):
            break
        operations.report(NAME, owner, done=n, total=len(targets), album=folder)
        try:
            path = library.album_dir(identity, library_id, folder)
        except ValueError as exc:
            # Moved or renamed since the list was read. Not the scanner's
            # failure, and the next run finds it where it is now.
            skipped.append(f"{folder}: {exc}")
            continue
        if not inbox.settled(path):
            skipped.append(f"{folder}: still arriving")
            continue
        try:
            # Album gain is measured across the folder, so two albums in
            # one would each get a figure for the pair.
            filer.require_one_album(path)
        except filer.NotEditable:
            skipped.append(f"{folder}: holds more than one album")
            continue
        try:
            measure(path)
            done += 1
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            failed.append(str(exc))
        # Every so often rather than once at the end: a run stopped or
        # killed an hour in has still measured what it measured, and
        # Navidrome only shows it after a scan.
        if done and done % 25 == 0:
            navidrome.notify()
    if done:
        navidrome.notify()
    return {"measured": done, "total": len(targets),
            "stopped": operations.stopping(NAME, owner),
            "failures": len(failed), "failed": failed[:20],
            "skipped": skipped[:20]}
