"""Writes Spotify metadata onto a downloaded file, as seed data for beets.

These tags are not the final product. Beets re-tags everything against
MusicBrainz on import, but it decides *which* release to match using the tags
already present. Handing it the real album, track number and ISRC is the
difference between an unattended import and one that stops to ask.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mutagen.id3 import (
    APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSRC, TXXX,
)
from mutagen.id3._util import ID3NoHeaderError

from . import covers

log = logging.getLogger("navidrome_companion.tagger")


def tag(path: Path, item: dict[str, Any], embed_cover: bool = True) -> None:
    """Replace the file's tags with Spotify's metadata."""
    try:
        tags = ID3(path)
        # yt-dlp and ffmpeg leave YouTube-derived frames behind; none of them
        # should survive into what beets reads. Cleared in memory, not on
        # disk: `delete()` stripped the file at once, so any failure before
        # the save below left it with no tags at all.
        tags.clear()
    except ID3NoHeaderError:
        tags = ID3()

    tags.add(TIT2(encoding=3, text=item["title"]))
    # The primary artist, not the full credit. beets decides an album is a
    # Various Artists release when its tracks disagree on this tag, and then
    # searches MusicBrainz for a compilation - so a single guest appearance
    # made the real record unfindable and the whole album went unmatched.
    # See spotify._primary_artist. A successful match writes MusicBrainz's
    # own credit back over this, guest included; only an import as-is keeps
    # what is written here.
    tags.add(TPE1(encoding=3, text=item.get("primary_artist") or item["artist"]))
    tags.add(TPE2(encoding=3, text=item["album_artist"]))
    tags.add(TALB(encoding=3, text=item["album"]))

    if item.get("track_no"):
        total = item.get("album_total")
        tags.add(TRCK(encoding=3, text=f"{item['track_no']}/{total}" if total
                      else str(item["track_no"])))
    if item.get("disc_no"):
        tags.add(TPOS(encoding=3, text=str(item["disc_no"])))
    if item.get("release_date"):
        tags.add(TDRC(encoding=3, text=item["release_date"]))
    if item.get("isrc"):
        tags.add(TSRC(encoding=3, text=item["isrc"]))
    if item.get("genre"):
        tags.add(TCON(encoding=3, text=item["genre"]))

    # Kept so a file can be traced back to its source after beets has renamed
    # and moved it.
    if item.get("spotify_id"):
        tags.add(TXXX(encoding=3, desc="SPOTIFY_ID", text=item["spotify_id"]))

    if embed_cover and item.get("cover_url"):
        # Squared because a YouTube cover is the video frame, bars and all.
        # A Spotify cover is square already and passes untouched.
        cover = covers.squared(item["cover_url"])
        if cover:
            data, mime = cover
            tags.add(APIC(encoding=3, mime=mime, type=3,
                          desc="Cover", data=data))

    tags.save(path, v2_version=4)
