"""Reads the beets library and proposes matches for what beets skipped.

Two jobs live here. Browsing goes through beets' own Library object rather
than raw SQL, so its query syntax works as documented and field access matches
what the CLI would give. Resolving uses beets' matcher directly to produce
candidate releases with their distances, but applies a choice by shelling out
to `beet import --search-id`, because that path already handles moving, art,
plugins and the library write correctly.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .beets_runner import BEETS_DIR, TIMEOUT, ensure_config
from .config import settings

log = logging.getLogger("download_center.beets_library")

AUDIO = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac"}


_plugins_loaded = False


def _prepare() -> None:
    """Point beets at our config and load its plugins.

    Loading plugins is not optional. Since beets 2.x the MusicBrainz backend
    is itself a plugin, so calling the matcher without this returns zero
    candidates for everything - the same trap as omitting it from the config.

    Setting up happens exactly once. Plugins register their own defaults into
    the config when they load, so clearing and re-reading it afterwards throws
    those defaults away while the plugins stay loaded - and the next match
    dies with "musicbrainz.extra_tags not found". The first call worked, every
    one after it failed.
    """
    global _plugins_loaded
    os.environ["BEETSDIR"] = str(BEETS_DIR)
    ensure_config()
    if _plugins_loaded:
        return

    from beets import config as beets_config, plugins

    beets_config.clear()
    beets_config.read()
    plugins.load_plugins()
    plugins.find_plugins()
    _plugins_loaded = True


def _library():
    _prepare()
    from beets import config as beets_config
    from beets.library import Library

    return Library(
        beets_config["library"].as_filename(),
        beets_config["directory"].as_filename(),
    )


def _album_card(album: Any) -> dict[str, Any]:
    return {
        "id": album.id,
        "album": album.album,
        "albumartist": album.albumartist,
        "year": album.year or None,
        "original_year": getattr(album, "original_year", None) or None,
        "tracks": len(album.items()),
        "mb_albumid": getattr(album, "mb_albumid", "") or None,
    }


def albums(query: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """List albums, optionally filtered with beets query syntax."""
    library = _library()
    try:
        found = list(library.albums(query or None))[:limit]
        return [_album_card(album) for album in found]
    finally:
        library._close()


def album_detail(album_id: int) -> dict[str, Any] | None:
    library = _library()
    try:
        album = library.get_album(album_id)
        if album is None:
            return None
        card = _album_card(album)
        card["tracks"] = [
            {
                "id": item.id,
                "title": item.title,
                "artist": item.artist,
                "track": item.track,
                "disc": item.disc,
                "length": item.length,
                "format": item.format,
                "bitrate": item.bitrate,
            }
            for item in sorted(album.items(), key=lambda i: (i.disc or 1, i.track or 0))
        ]
        return card
    finally:
        library._close()


def stats() -> dict[str, Any]:
    library = _library()
    try:
        items = list(library.items())
        return {
            "albums": len(list(library.albums())),
            "tracks": len(items),
            "artists": len({i.albumartist or i.artist for i in items}),
            "seconds": sum(i.length or 0 for i in items),
        }
    finally:
        library._close()


# --- the inbox: things beets declined to match ----------------------------

def _audio_in(path: Path) -> list[Path]:
    return sorted(p for p in path.rglob("*") if p.suffix.lower() in AUDIO)


def inbox() -> list[dict[str, Any]]:
    """Everything still sitting in staging.

    A successful import moves its files out, so whatever remains is precisely
    what beets could not confidently identify.
    """
    entries: list[dict[str, Any]] = []

    for directory in sorted(settings.albums_dir.glob("*")):
        if directory.is_dir():
            files = _audio_in(directory)
            if files:
                entries.append({
                    "path": str(directory),
                    "name": directory.name,
                    "kind": "album",
                    "tracks": len(files),
                })

    for track in sorted(settings.singles_dir.glob("*")):
        if track.is_file() and track.suffix.lower() in AUDIO:
            entries.append({
                "path": str(track),
                "name": track.stem,
                "kind": "single",
                "tracks": 1,
            })

    return entries


def _within_staging(path: Path) -> bool:
    """Refuse to act on anything outside the staging tree.

    Every path here arrives from an HTTP request, so it is treated as
    untrusted no matter that the UI only ever sends values it was given.
    """
    try:
        resolved = path.resolve()
        root = settings.output_dir.resolve()
        return root in resolved.parents
    except OSError:
        return False


def _checked(target: str) -> Path:
    path = Path(target)
    if not _within_staging(path) or not path.exists():
        raise ValueError("That path is not in the staging area.")
    return path


def candidates(target: str, limit: int = 5) -> dict[str, Any]:
    """Ask beets' matcher what this could be, without changing anything."""
    path = _checked(target)

    _prepare()
    from beets import autotag
    from beets.library import Item

    files = _audio_in(path) if path.is_dir() else [path]
    if not files:
        raise ValueError("No audio files there.")

    items = [Item.from_path(str(f)) for f in files]
    cur_artist, cur_album, proposal = autotag.tag_album(items)

    results = []
    for match in proposal.candidates[:limit]:
        info = match.info
        results.append({
            "album_id": info.album_id,
            "album": info.album,
            "artist": info.artist,
            "year": info.year,
            "country": getattr(info, "country", None),
            "label": getattr(info, "label", None),
            "media": getattr(info, "media", None),
            "track_count": len(info.tracks or []),
            # beets' own disagreement score, 0..1. Lower is a better match.
            "distance": round(float(match.distance), 4),
            "missing": len(match.extra_tracks),
            "unmatched": len(match.extra_items),
        })

    return {
        "path": str(path),
        "current_artist": cur_artist,
        "current_album": cur_album,
        "file_count": len(files),
        "recommendation": proposal.recommendation.name,
        "candidates": results,
    }


def _beet(arguments: list[str]) -> tuple[bool, str]:
    environment = {**os.environ, "BEETSDIR": str(BEETS_DIR)}
    try:
        result = subprocess.run(
            [sys.executable, "-m", "beets", *arguments], env=environment, capture_output=True,
            text=True, timeout=TIMEOUT, check=False,
        )
    except FileNotFoundError:
        return False, "beets is not available in this environment"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {TIMEOUT}s"
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output[-500:]


# Quiet imports only auto-apply a *strong* recommendation, so naming a release
# is not by itself enough to make one stick - a 94% match still gets skipped.
# Raising the threshold for this one invocation says "the choice has already
# been made", which is exactly true when the release came from the user.
_FORCE_OVERLAY = """match:
  strong_rec_thresh: 1.0
"""


def apply(target: str, album_id: str | None = None, as_is: bool = False) -> dict[str, Any]:
    """Import a staged path, either forcing a release or taking it as-is."""
    path = _checked(target)

    singleton = path.is_file()
    overlay: Path | None = None
    arguments: list[str] = []

    if as_is:
        # Keep whatever tags the files already carry and skip matching.
        arguments += ["import", "-qs" if singleton else "-q", "-A"]
    elif album_id:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        )
        handle.write(_FORCE_OVERLAY)
        handle.close()
        overlay = Path(handle.name)
        arguments += ["-c", str(overlay), "import",
                      "-qs" if singleton else "-q", "--search-id", album_id]
    else:
        arguments += ["import", "-qs" if singleton else "-q"]

    arguments.append(str(path))

    try:
        ok, output = _beet(arguments)
    finally:
        if overlay:
            overlay.unlink(missing_ok=True)

    if ok and "Skipping" in output:
        ok = False
        output = "beets still declined to match this."

    log.info("apply %s -> %s", path.name, "ok" if ok else output[:120])
    return {"ok": ok, "output": output}


def discard(target: str) -> dict[str, Any]:
    """Delete a staged item without importing it."""
    path = _checked(target)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return {"ok": True}
