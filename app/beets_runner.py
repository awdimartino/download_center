"""Asking beets about one album, because somebody asked it to.

Beets used to run unattended over everything that arrived, and it was the
gate: what it would not match did not enter the library. Measured, it refused
82% of what it was handed, so it is not a gate any more. The filer puts music
in the library and this is consulted afterwards, by hand, one album at a time
from the review page.

What is left is therefore small: write a workspace its config, ask what a
folder would match against, and apply the release a person picked. There is
no sweep, no refusal memory and no import lock, because there is no longer
anything running on its own that those existed to coordinate.

Matching a whole album at once - on track count, ordering, durations and
artist together - is far more reliable than per-track identifier lookups, so
the unit here is a directory, which is also what the filer guarantees is one
album.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import subprocess
from pathlib import Path
from typing import Any

from . import navidrome, workspace
from .config import CONFIG_DIR, settings

log = logging.getLogger("download_center.beets")

# Kept for the one-off migration of the single-user layout; every other
# reference goes through a workspace.
LEGACY_BEETS_DIR = CONFIG_DIR / "beets"

# Long enough for art fetching and MusicBrainz lookups on a slow connection,
# short enough that a wedged retag cannot hold a thread forever.
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
  # A retag rewrites the file where it is, and moves it only if the artist or
  # album changed - which is the one thing allowed to move a filed track.
  move: yes
  write: yes
  # Never prompt: there is no terminal on the other end of this. A release
  # is only ever applied because somebody picked it from the candidate list.
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

# Switching chroma on quietly made every match worse, and this puts it back.
#
# Beets penalises a candidate whose data source differs from the file's - but
# only once more than one metadata source plugin is loaded, which is exactly
# what adding chroma did. The files carry no data_source tag at all, so from
# then on *every* track scored a mismatch against MusicBrainz. Measured on
# C418's Minecraft Volume Alpha, a 24 file folder against the 24 track
# release, titles and track numbers matching exactly:
#
#     chroma off                     distance 0.0003   strong   imports
#     chroma on                      distance 0.1113   medium   skipped
#     chroma on, this penalty at 0   distance 0.0002   strong   imports
#
# The penalty read is the one belonging to the source being matched against,
# so it is musicbrainz's value that has to be zeroed, not chroma's. Nothing
# is lost by it here: MusicBrainz is the only release source configured, so
# "the data came from somewhere else" is not a distinction this setup can
# draw, and charging 0.5 for it only ever punished correct matches.
musicbrainz:
  data_source_mismatch_penalty: 0
"""


def _paths_block(text: str) -> tuple[int, int] | None:
    """Where the `paths:` mapping starts and ends, as line indices.

    Line-based rather than parsed, because the file is a hand-editable
    config and round-tripping it through a YAML loader would rewrite
    everything else in it - comments included, and the comments here are
    most of what the file is for.
    """
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines)
                  if line.startswith("paths:")), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and (not lines[end].strip()
                                or lines[end][:1] in (" ", "\t")):
        end += 1
    return start, end


def _repair_paths(space: workspace.Workspace) -> bool:
    """Bring an existing config's path template up to the current one.

    `ensure_config` deliberately never overwrites a config, and
    `adopt_legacy` rewrites only the three absolute paths inside one. So a
    workspace that inherited the single-user installation still files with
    the template that installation had:

        $albumartist/$album%aunique{} ($original_year)/$track $title

    Both of those are exactly what the current template's own comment says
    must not be there. `%aunique{}` and the year put two pressings of one
    record in two folders, and the folder name no longer matches what the
    filer writes - so a retag confirmed in the review page moves the files
    somewhere the frozen layout does not have, and leaves the album's real
    directory behind, empty.

    Only the `paths:` block is touched. `directory`, the database and log
    locations, and any other hand edit are left exactly as they are.
    """
    text = space.beets_config.read_text(encoding="utf-8")
    here, wanted = _paths_block(text), _paths_block(DEFAULT_CONFIG)
    if here is None or wanted is None:
        return False

    lines = text.splitlines(keepends=True)
    current = "".join(lines[here[0]:here[1]])
    replacement = "".join(DEFAULT_CONFIG.splitlines(keepends=True)[wanted[0]:wanted[1]])
    if current == replacement:
        return False

    lines[here[0]:here[1]] = [replacement]
    space.beets_config.write_text("".join(lines), encoding="utf-8")
    log.info("updated the beets path template for %s; it still had the "
             "single-user one, which files albums somewhere the library "
             "layout does not have", space.username)
    return True


def ensure_config(space: workspace.Workspace) -> Path:
    """Create this person's beets config on first use; never overwrite it.

    The destination is filled in from their Navidrome library, so adding a
    user is nothing more than them signing in once.

    The one exception is the path template, which decides where beets puts a
    file and therefore has to agree with the filer - see `_repair_paths`.
    """
    space.prepare()
    if space.beets_config.exists():
        _repair_paths(space)
    else:
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
    """Retag an album as the release a person picked.

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

    ensure_config(space)
    before = _library_size(space)
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
        log.warning("choosing a release failed for %s: %s", path.name, output)
        return {"ran": True, "imported": 0, "skipped": 0,
                "failed": [f"{path.name}: {detail}"]}

    # Asked of the library, not of the subprocess: it reports what it chose,
    # and this reports what actually arrived.
    if _library_size(space) <= before:
        log.info("chosen release %s filed nothing for %s",
                 release_id, path.name)
        return {"ran": True, "imported": 0, "skipped": 1, "failed": [],
                "chosen": release_id}

    # Identity is not written here. The caller re-points the album UUID
    # through the registry - see `filer.after_retag` - because which album
    # these files are on is a question the registry answers and beets has
    # no idea it is being asked.
    navidrome.notify()
    log.info("retagged %s as %s", path.name, release_id)
    return {"ran": True, "imported": 1, "skipped": 0, "failed": [],
            "chosen": release_id}
