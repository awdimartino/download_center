"""Walking a library the way Navidrome sees it.

Anything that reads every file under a library root goes through here, so
they all agree on what "the library" is. The quarantine sits inside each
library root - moving a file there has to be a rename on one filesystem -
and a plain `rglob` walked straight into it: the survey counted set-aside
copies as part of the album they were removed from, `unfuse --apply` then
retagged the live tracks to the quarantined copies' older UUID, and Health
warned for ever about files no scan would ever index.

Navidrome skips a directory holding an *empty* `.ndignore` (a non-empty one
is a list of patterns, which is not modelled here), so this skips the same
directories, and the quarantine by name even if its marker has gone missing.
"""

from __future__ import annotations

import os
from pathlib import Path

QUARANTINE_NAME = "duplicates-removed"
NDIGNORE = ".ndignore"


def _ignored(directory: Path) -> bool:
    marker = directory / NDIGNORE
    try:
        return marker.is_file() and marker.stat().st_size == 0
    except OSError:
        return False


def library_files(root: Path) -> list[Path]:
    """Every file under `root` that Navidrome would scan, sorted."""
    found: list[Path] = []
    for directory, subdirectories, files in os.walk(root):
        here = Path(directory)
        subdirectories[:] = [
            name for name in subdirectories
            if not (here == root and name == QUARANTINE_NAME)
            and not _ignored(here / name)]
        found.extend(here / name for name in files)
    return sorted(found)
