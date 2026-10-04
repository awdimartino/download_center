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
import sys
import subprocess
from pathlib import Path
from typing import Any

from . import workspace
from .config import settings

log = logging.getLogger("navidrome_companion.beets")

# Long enough for art fetching and MusicBrainz lookups on a slow connection,
# short enough that a wedged retag cannot hold a thread forever.
TIMEOUT = 900

DEFAULT_CONFIG = """\
# Written by Navidrome Companion on first run. Edit freely - it is never
# overwritten, and the container reads it on every import.

directory: __DIRECTORY__
library: __LIBRARY__

ui:
  # Beets colourises --pretend output even when it is redirected to a file,
  # embedding escape codes inside the paths it prints. Anything parsing that
  # output then matches nothing, silently.
  color: no

import:
  # Beets tags; it does not move. `write: yes` rewrites the file in place and
  # `move`/`copy` off leave it exactly where the filer put it, so there is
  # only ever one thing that decides where a track lives.
  #
  # This used to be `move: yes`, and that was a second layout implementation:
  # beets' path template has no disc prefix, no filename sanitising and no
  # rule for a track with no number, so a confirmed retag landed files at
  # paths the filer would never produce. It also meant success had to be
  # inferred from beets' row count growing, which a *re*-tag never does.
  move: no
  copy: no
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

# Unused: with `move` and `copy` off, beets never writes a path. Kept so a
# config that is edited by hand still reads sensibly, and so switching move
# back on does not immediately disagree with `filer.destination`.
paths:
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


def ensure_config(space: workspace.Workspace) -> Path:
    """Create this person's beets config on first use; never overwrite it.

    The destination is filled in from their Navidrome library, so adding a
    user is nothing more than them signing in once.

    An inherited config keeps whatever path template it had, and that is now
    harmless: with `move` and `copy` off, beets never writes a path. The
    filer decides where every file lives.
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


# --- choosing a match by hand ----------------------------------------------
# The other half of the escape hatch. Import as-is says "file it with what it
# has"; this says "it is *this* release, use that". Wanted where the seeded
# tags are wrong rather than merely unconfirmed.

# Beets gets a while: a candidate lookup is several MusicBrainz round trips
# and this runs on a Raspberry Pi. Shorter than the import timeout because
# nothing is being written and a person is watching a spinner.
MATCH_TIMEOUT = 180


def candidates(space: workspace.Workspace, path: Path) -> dict[str, Any]:
    """What beets would match this path against, in its own order."""
    if not settings.beets_enabled:
        # The switch has to cover this as well as applying a choice. Looking
        # for candidates is the expensive half - several MusicBrainz round
        # trips on a Raspberry Pi - so a setting that only stopped the cheap
        # half was not the lever it claimed to be.
        return {"error": "Matching is turned off in Settings.",
                "candidates": []}
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

    # The subprocess's own exit code, not a count of what beets indexed.
    # Counting rows answered "did an import file something new", which a
    # retag never does: beets already has these files, so the count never
    # grew, every retag reported "the album is unchanged", and the album
    # UUID was never re-pointed - leaving the registry describing an album
    # that no longer exists under that name.
    #
    # Identity is not written here either. The caller re-points the album
    # UUID through the registry - see `filer.after_retag` - because which
    # album these files are on is a question the registry answers and beets
    # has no idea it is being asked.
    log.info("retagged %s as %s", path.name, release_id)
    return {"ran": True, "imported": 1, "skipped": 0, "failed": [],
            "chosen": release_id}
