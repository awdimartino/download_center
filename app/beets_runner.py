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
import sys
import subprocess
from pathlib import Path
from typing import Any

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

paths:
  default: $albumartist/$album%aunique{} ($original_year)/$track $title
  singleton: Singles/$artist - $title
  comp: Various Artists/$album%aunique{} ($original_year)/$track $title

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


def import_paths(published: list[Path]) -> dict[str, Any]:
    """Import freshly published paths, returning a summary for the UI.

    Only what this job produced is imported, never the whole staging tree, so
    a concurrent job or another tool writing there is left alone.
    """
    if not settings.beets_enabled:
        return {"ran": False, "reason": "disabled"}

    ensure_config()
    imported, skipped, failed = 0, 0, []

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

    return {
        "ran": True,
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
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
