"""Ask beets what it would match a staged path against, and print it as JSON.

Run as a subprocess with BEETSDIR pointing at one person's workspace:

    python -m app.beets_match <path>                 what would it match?
    python -m app.beets_match --apply <id> <path>    file it as that release

It is a subprocess rather than a function call because beets' configuration
and its plugin registry are process-global singletons, read once from
BEETSDIR and never rebound. Importing beets into the application would tie
the whole process to whichever workspace happened to be first, which for an
application whose entire point is that each person has their own library is
not a trade worth making. `beets_runner` already shells out for the same
reason.

This is beets' own matcher, asked politely instead of being told to decide.
Nothing is written and nothing moves: the caller gets the candidate list
that `quiet_fallback: skip` threw away, and a person picks from it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# How many candidates are worth showing. Beets returns five by default and
# the tail of that list is noise - by the third the distances are usually
# telling you the answer is not here at all.
MAX_CANDIDATES = 5

AUDIO = (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".aif")


def _album_candidate(match: Any) -> dict[str, Any]:
    info = match.info
    return {
        "id": info.album_id,
        "title": info.album,
        "artist": info.artist,
        "year": info.year,
        "tracks": len(info.tracks or []),
        "distance": round(float(match.distance.distance), 4),
        "source": getattr(info, "data_source", "MusicBrainz"),
        # What beets held against it, in its own words. The reason a match
        # was refused is the most useful thing on the row: "missing tracks"
        # and "title mismatch" call for different answers.
        "penalties": sorted(match.distance.keys()),
    }


def _track_candidate(match: Any) -> dict[str, Any]:
    info = match.info
    return {
        "id": info.track_id,
        "title": info.title,
        "artist": info.artist,
        "year": getattr(info, "year", None),
        "album": getattr(info, "album", None),
        "length": round(info.length) if info.length else None,
        "distance": round(float(match.distance.distance), 4),
        "source": getattr(info, "data_source", "MusicBrainz"),
        "penalties": sorted(match.distance.keys()),
    }


def _recommendation(proposal: Any) -> str:
    """How sure beets was, as a word.

    `Recommendation` is an IntEnum, so str() of it is "2" - a number whose
    meaning lives in beets' source rather than on the screen.
    """
    return getattr(proposal.recommendation, "name", str(proposal.recommendation))


def candidates(path: Path) -> dict[str, Any]:
    # Imported here, not at module scope: loading beets costs a second and
    # this module is imported by the test suite to check its shape.
    from beets import autotag, config, plugins
    from beets.library import Item

    config.read()
    plugins.load_plugins()

    if path.is_dir():
        files = sorted(p for p in path.rglob("*")
                       if p.is_file() and p.suffix.lower() in AUDIO)
        if not files:
            return {"kind": "album", "candidates": [], "reason": "no audio files"}
        items = [Item.from_path(str(p)) for p in files]
        artist, album, proposal = autotag.tag_album(items)
        return {
            "kind": "album",
            "artist": artist,
            "album": album,
            "recommendation": _recommendation(proposal),
            "candidates": [_album_candidate(m)
                           for m in proposal.candidates[:MAX_CANDIDATES]],
        }

    item = Item.from_path(str(path))
    proposal = autotag.tag_item(item)
    return {
        "kind": "single",
        "artist": item.artist,
        "album": item.title,
        "recommendation": _recommendation(proposal),
        "candidates": [_track_candidate(m)
                       for m in proposal.candidates[:MAX_CANDIDATES]],
    }


def apply_choice(path: Path, chosen_id: str) -> dict[str, Any]:
    """File `path` as the release someone picked from the candidate list.

    Beets is *asked*, through the extension point its own test suite uses:
    `ImportSession.choose_match` returning a match applies that match, no
    matter what the confidence numbers say. A person looking at the album
    has already answered the question the numbers exist to answer.

    The alternative was telling beets to trust everything - raising
    `strong_rec_thresh` and lifting the `max_rec` caps in a config layered
    over the workspace's - and it does not even work: measured against a
    real staged album, `--search-id` with both loosened still filed nothing,
    because quiet mode applies only on a *strong* recommendation and missing
    tracks cap it at medium regardless. Lying to beets about its own
    thresholds would also have left that lie one config mistake away from
    the unattended sweep.
    """
    from beets import config, importer, plugins
    from beets.library import Library

    config.read()
    plugins.load_plugins()

    lib = Library(config["library"].as_filename(),
                  config["directory"].as_filename())
    picked: list[str] = []

    class ChosenSession(importer.ImportSession):
        """Answers beets' one question with the answer already given."""

        def should_resume(self, path: Any) -> bool:
            return False

        def _pick(self, task: Any, attribute: str) -> Any:
            for match in task.candidates:
                if getattr(match.info, attribute, None) == chosen_id:
                    picked.append(chosen_id)
                    return match
            # The chosen release was not among the candidates - it has been
            # deleted at MusicBrainz, or the lookup failed. Skipping leaves
            # the files exactly where they were.
            return importer.Action.SKIP

        def choose_match(self, task: Any) -> Any:
            return self._pick(task, "album_id")

        def choose_item(self, task: Any) -> Any:
            return self._pick(task, "track_id")

    # A loose file is a singleton and a directory is an album, the same rule
    # the rest of the pipeline follows.
    config["import"]["singletons"] = path.is_file()
    # Ask MusicBrainz for this release directly rather than searching and
    # hoping it comes back in the top five.
    config["import"]["search_ids"] = [chosen_id]

    ChosenSession(lib, None, [str(path)], None).run()
    return {"applied": bool(picked), "chosen": chosen_id}


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--apply":
        chosen, target = sys.argv[2], Path(sys.argv[3])
        if not target.exists():
            print(json.dumps({"error": f"{target} is not there"}))
            return 1
        try:
            print(json.dumps(apply_choice(target, chosen)))
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
            return 1
        return 0

    if len(sys.argv) != 2:
        print(json.dumps({"error": "usage: beets_match [--apply <id>] <path>"}))
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print(json.dumps({"error": f"{path} is not there"}))
        return 1
    try:
        print(json.dumps(candidates(path)))
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        # Printed as JSON rather than raised, so the caller gets a reason
        # instead of a traceback on stderr and an empty list.
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
