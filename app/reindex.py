"""Adopt an existing, already-organised library into beets.

Run once, when moving from a beets install elsewhere. Carrying the old
`library.db` across is the obvious approach and the wrong one: it stores
absolute paths that will not match wherever this container mounts the library,
and opening a database written by an older beets applies one-way schema
migrations to the only copy you have.

Re-indexing avoids both. The database is only an index - the files are the
truth, and every tag beets wrote is still on them. Reading them back rebuilds
the index in minutes with no network traffic, no matching, and no risk to the
files themselves.

Autotagging is off on purpose. These files have already been matched once;
running them past MusicBrainz again would re-decide releases that were settled
long ago, and at several thousand albums against a rate-limited API it would
take hours to do damage.

    python -m app.reindex --apply

Nothing is written without --apply.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .beets_runner import BEETS_DIR, ensure_config
from .config import settings

# Moving and copying are both disabled: the files are already where they
# belong, and this must not reorganise a library it is only reading. The
# overlay is temporary so the real config keeps `move: yes` for imports.
OVERLAY = """\
import:
  copy: no
  move: no
  write: no
  autotag: no
  quiet: yes
  incremental: no
  resume: no
"""


def reindex(root: Path, apply: bool) -> int:
    ensure_config()

    if not root.is_dir():
        print(f"!! not a directory: {root}", file=sys.stderr)
        return 1

    library_db = BEETS_DIR / "library.db"
    if library_db.exists() and apply:
        print(f"!! {library_db} already exists.\n"
              f"   Move it aside first - this is meant for a fresh index.",
              file=sys.stderr)
        return 1

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(OVERLAY)
        overlay_path = handle.name

    command = [sys.executable, "-m", "beets", "--config", overlay_path,
               "import", "-A", "-q", str(root)]
    environment = {**os.environ, "BEETSDIR": str(BEETS_DIR)}

    print(f"{'APPLYING' if apply else 'DRY RUN'} - reindexing {root}")
    print("  " + " ".join(command) + "\n")
    if not apply:
        print("Nothing written. Re-run with --apply to build the index.")
        os.unlink(overlay_path)
        return 0

    try:
        result = subprocess.run(command, env=environment, check=False)
    finally:
        os.unlink(overlay_path)

    if result.returncode != 0:
        print(f"\n!! beets exited {result.returncode}", file=sys.stderr)
        return result.returncode

    print("\nIndexed. Check the count matches what is on disk:")
    print(f"  python -m beets stats")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=settings.music_dir,
                        help="the existing library (default: music_dir)")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    return reindex(args.root, args.apply)


if __name__ == "__main__":
    sys.exit(main())
