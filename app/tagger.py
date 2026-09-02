"""Writes Spotify metadata onto a downloaded file, as seed data for beets.

These tags are not the final product. Beets re-tags everything against
MusicBrainz on import, but it decides *which* release to match using the tags
already present. Handing it the real album, track number and ISRC is the
difference between an unattended import and one that stops to ask.
"""

from __future__ import annotations

import logging
import urllib.request
from pathlib import Path
from typing import Any

from mutagen.id3 import (
    APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSRC, TXXX,
)
from mutagen.id3._util import ID3NoHeaderError

log = logging.getLogger("download_center.tagger")


def _fetch_cover(url: str) -> bytes | None:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "download-center"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.read()
    except Exception as exc:
        log.debug("cover fetch failed for %s: %s", url, exc)
        return None


def tag(path: Path, item: dict[str, Any], embed_cover: bool = True) -> None:
    """Replace the file's tags with Spotify's metadata."""
    try:
        tags = ID3(path)
        # yt-dlp and ffmpeg leave YouTube-derived frames behind; none of them
        # should survive into what beets reads.
        tags.delete()
    except ID3NoHeaderError:
        tags = ID3()

    tags.add(TIT2(encoding=3, text=item["title"]))
    tags.add(TPE1(encoding=3, text=item["artist"]))
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
        cover = _fetch_cover(item["cover_url"])
        if cover:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3,
                          desc="Cover", data=cover))

    tags.save(path, v2_version=4)
