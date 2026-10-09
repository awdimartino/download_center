"""Which tracks of an album the library does not have.

The reference tracklist comes from MusicBrainz when the album's files name a
release there - the exact edition, chosen when the album was matched - and
from Spotify otherwise. Either way the other editions are offered, because
"missing" depends on which one you meant: the deluxe edition is missing its
bonus tracks from a standard copy that is complete.

A track counts as held when one of the album's files is that recording
(MusicBrainz recording ids, where both sides have them), or failing that has
the same title once the version notes are set aside. One file answers for
one track at most, so two takes with one title do not cover each other.
"""

from __future__ import annotations

import collections
import logging
import re
import sqlite3
from typing import Any

from rapidfuzz import fuzz

from . import library, matcher, musicbrainz, navidrome, registry, spotify

log = logging.getLogger("navidrome_companion.albumcheck")

# Titles this alike, once normalised, are one song.
TITLE_SAME = 92
# Spotify albums opened to compare, when there is no MusicBrainz release.
SPOTIFY_OPENED = 5
EDITIONS_SHOWN = 25
# MusicBrainz's titles for a track nobody has named. Searching for one
# would download whatever else is called "[unknown]".
UNTITLED = {"[unknown]", "[untitled]", ""}

_VERSION_NOTE = re.compile(
    r"\s*(?:[\(\[][^\)\]]*(?:remaster|version|edit|mono|stereo|mix|deluxe|"
    r"bonus|live|demo|instrumental)[^\)\]]*[\)\]]|\s-\s.*(?:remaster|version|"
    r"edit|mono|stereo|mix).*)$", re.I)


def same_title(text: str) -> str:
    """A title in the form two copies of one song share."""
    return matcher.normalise(_VERSION_NOTE.sub("", text or ""))


def _held(identity: navidrome.Identity, library_id: int,
          folder: str) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    """The album as the library has it: its names, its files with their
    MusicBrainz ids, and the release ids those files carry (most common
    first, as `release` and `group`)."""
    album = library.tracks(identity, library_id, folder)
    ids = [t["id"] for t in album["items"]]
    marks = {}
    try:
        connection = navidrome.open_db()
        with connection:
            columns = navidrome.columns_of(connection, "media_file")
            wanted = [c for c in ("mbz_recording_id", "mbz_album_id",
                                  "mbz_release_group_id") if c in columns]
            if wanted:
                inside = ",".join("?" * len(ids))
                selected = ", ".join(f"coalesce({c}, '')" for c in wanted)
                for row in connection.execute(
                        f"select id, {selected} from media_file"
                        f" where id in ({inside})", ids):
                    marks[row[0]] = dict(zip(wanted, row[1:], strict=True))
    except (navidrome.Unavailable, sqlite3.Error) as exc:
        log.warning("cannot read MusicBrainz ids: %s", exc)
    files = [{**t, **marks.get(t["id"], {})} for t in album["items"]]
    releases = collections.Counter(f.get("mbz_album_id") for f in files
                                   if f.get("mbz_album_id"))
    groups = collections.Counter(f.get("mbz_release_group_id") for f in files
                                 if f.get("mbz_release_group_id"))
    found = {}
    if releases:
        found["release"] = releases.most_common(1)[0][0]
    if groups:
        found["group"] = groups.most_common(1)[0][0]
    return album, files, found


def compare(reference: list[dict[str, Any]],
            files: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Mark each reference track held or missing; also count the files that
    matched nothing (tracks not on this edition)."""
    unused = list(files)
    marked = []
    by_recording = {f.get("mbz_recording_id"): f for f in files
                    if f.get("mbz_recording_id")}
    for track in reference:
        match = by_recording.get(track.get("recording_id") or "")
        if match is None or match not in unused:
            want = same_title(track["title"])
            match = next((f for f in unused
                          if fuzz.ratio(same_title(f["title"]), want) >= TITLE_SAME), None)
        if match is not None:
            unused.remove(match)
        marked.append({**track, "held": match is not None,
                       "held_as": match["title"] if match else None})
    return marked, len(unused)


# --- the reference, from either source -------------------------------------

def _mb_label(one: dict[str, Any]) -> str:
    parts = [one["title"]]
    if one.get("disambiguation"):
        parts[0] += f" ({one['disambiguation']})"
    parts += [one.get("date", "")[:4], one.get("country", ""),
              " + ".join(one.get("formats") or []),
              f"{one.get('track_count', len(one.get('tracks') or []))} tracks"]
    return " · ".join(p for p in parts if p)


def _from_musicbrainz(release_id: str, group: str | None) -> dict[str, Any]:
    chosen = musicbrainz.release(release_id)
    group = group or chosen.get("release_group")
    others = []
    if group:
        try:
            others = musicbrainz.editions(group)
        except musicbrainz.Unavailable as exc:
            log.info("no editions for %s: %s", group, exc)
    # One entry per edition that differs in what a person would choose by:
    # MusicBrainz lists every pressing, and twenty CDs of one tracklist are
    # one choice.
    seen: set[tuple] = set()
    editions = []
    for one in sorted(others, key=lambda e: e.get("date") or "9999"):
        key = (one["title"].lower(), one.get("disambiguation", "").lower(),
               one["track_count"])
        if key in seen and one["id"] != release_id:
            continue
        seen.add(key)
        editions.append({"id": f"mb:{one['id']}", "label": _mb_label(one)})
    if not any(e["id"] == f"mb:{release_id}" for e in editions):
        editions.insert(0, {"id": f"mb:{release_id}",
                            "label": _mb_label({**chosen, "track_count": len(chosen["tracks"])})})
    return {"source": "musicbrainz", "edition": f"mb:{release_id}",
            "title": chosen["title"], "label": _mb_label(
                {**chosen, "track_count": len(chosen["tracks"])}),
            "url": f"https://musicbrainz.org/release/{release_id}",
            "editions": editions[:EDITIONS_SHOWN], "tracks": chosen["tracks"]}


def _spotify_tracks(album_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    detail = spotify.album_detail(album_id)
    tracks = [{"disc": t.get("disc_no") or 1, "number": t.get("track_no") or 0,
               "title": t["name"], "artist": t.get("artist") or detail["artist"],
               "length_ms": t.get("duration_ms"), "spotify_id": t["id"]}
              for t in detail["tracks"]]
    return detail, tracks


def _spotify_label(detail: dict[str, Any], count: int) -> str:
    return " · ".join(p for p in (detail["name"], detail.get("year") or "",
                                  detail.get("type") or "", f"{count} tracks") if p)


def _from_spotify(album: dict[str, Any], files: list[dict[str, Any]],
                  edition: str | None) -> dict[str, Any]:
    """The Spotify album sharing the most titles with the files, or the one
    asked for, with the other candidates as editions."""
    artist, name = album["artist"], album["album"]
    found = spotify.search(f'album:"{name}" artist:"{artist}"', "album", 10) \
        or spotify.search(f"{artist} {name}", "album", 10)
    want_artist = registry.normalize(artist)
    candidates = [a for a in found if a and any(
        registry.normalize(x.get("name") or "") == want_artist
        for x in a.get("artists") or [])]
    candidates.sort(key=lambda a: -fuzz.ratio(same_title(a.get("name") or ""),
                                              same_title(name)))
    opened = []
    for candidate in candidates[:SPOTIFY_OPENED]:
        detail, tracks = _spotify_tracks(candidate["id"])
        marked, _extra = compare(tracks, files)
        opened.append((sum(t["held"] for t in marked), -abs(len(tracks) - len(files)),
                       detail, tracks))
    asked = edition[3:] if edition and edition.startswith("sp:") else None
    if asked and not any(d["id"] == asked for _h, _d, d, _t in opened):
        detail, tracks = _spotify_tracks(asked)
        opened.append((0, 0, detail, tracks))
    if not opened:
        raise LookupError(f"Spotify has no album called {name!r} by {artist}.")
    chosen = next((o for o in opened if o[2]["id"] == asked), None) \
        or max(opened, key=lambda o: (o[0], o[1]))
    _held_n, _fit, detail, tracks = chosen
    return {"source": "spotify", "edition": f"sp:{detail['id']}",
            "title": detail["name"], "label": _spotify_label(detail, len(tracks)),
            "url": detail.get("url"),
            "editions": [{"id": f"sp:{d['id']}", "label": _spotify_label(d, len(t))}
                         for _h, _f, d, t in opened],
            "tracks": tracks}


def missing(identity: navidrome.Identity, library_id: int, folder: str,
            edition: str | None = None) -> dict[str, Any]:
    """The album's reference tracklist, each track marked held or missing.

    `edition` is an id from an earlier answer's `editions` ("mb:<release>"
    or "sp:<album>"); without one, the files' own MusicBrainz release, or
    the Spotify album that fits them best.
    """
    album, files, marks = _held(identity, library_id, folder)
    reference = None
    problems = []
    wants_mb = edition.startswith("mb:") if edition else bool(marks.get("release"))
    if wants_mb:
        release_id = edition[3:] if edition else marks["release"]
        try:
            reference = _from_musicbrainz(release_id, marks.get("group"))
        except musicbrainz.Unavailable as exc:
            problems.append(str(exc))
    if reference is None:
        try:
            reference = _from_spotify(album, files, edition)
        except (spotify.ResolveError, LookupError) as exc:
            problems.append(str(exc))
        except Exception as exc:
            problems.append(f"Spotify: {exc}"[:200])
    if reference is None:
        raise LookupError(" ".join(problems) or "No tracklist found for this album.")

    marked, extra = compare(reference.pop("tracks"), files)
    # Missing from this folder is not always missing from the library: a
    # track filed as its own single, or on a compilation, would download as
    # a second copy. Asked of the same index the Download tab's "In library"
    # badge reads.
    everywhere = navidrome.held_in(library_id)
    for track in marked:
        track["downloadable"] = (track["title"].strip().lower()
                                 not in UNTITLED)
        track["elsewhere"] = not track["held"] and any(
            registry.recording_key(credit, track["title"]) in everywhere
            for credit in {track.get("artist") or "", album["artist"]} if credit)
    return {**reference, "album": album["album"], "artist": album["artist"],
            "tracks": marked,
            "missing": sum(not t["held"] and not t["elsewhere"] for t in marked),
            "elsewhere": sum(t["elsewhere"] for t in marked),
            "not_on_edition": extra, "problems": problems}


# --- downloading what is missing ---------------------------------------------

def _spotify_match(track: dict[str, Any], album_name: str) -> dict[str, Any] | None:
    """The Spotify track for a reference track: its own id when the
    reference is Spotify's, otherwise a search, preferring a copy on an
    album of the same name."""
    sp = spotify.client()
    if track.get("spotify_id"):
        return spotify.to_track(sp.track(track["spotify_id"]))
    title = (track.get("title") or "").replace('"', " ")
    artist = (track.get("artist") or "").replace('"', " ")
    found = [t for t in spotify.search(f'track:"{title}" artist:"{artist}"', "track", 10)
             if t and fuzz.ratio(same_title(t.get("name") or ""), same_title(title)) >= TITLE_SAME]
    if not found:
        return None
    want_album = same_title(album_name)
    found.sort(key=lambda t: -fuzz.ratio(
        same_title((t.get("album") or {}).get("name") or ""), want_album))
    return spotify.to_track(found[0])


def album_items(album: dict[str, Any], wanted: list[dict[str, Any]],
                total: int) -> tuple[str, str, list[dict[str, Any]]]:
    """(kind, job title, tracks) for downloading `wanted` into `album`.

    Every track is tagged with the library's own album and album artist,
    so it files beside the tracks already there, numbered as the reference
    edition numbers it. Spotify supplies what it can - ISRC, cover, the
    exact running time - and a track Spotify does not have is still
    queued, from the reference's title, artist and length: the download
    searches YouTube Music, SoundCloud and Bandcamp by those alone.
    """
    tracks = []
    for want in wanted:
        try:
            found = _spotify_match(want, album["album"])
        except Exception as exc:
            log.info("no Spotify lookup for %s: %s", want.get("title"), exc)
            found = None
        base = found or {
            "spotify_id": None, "isrc": None, "title": want["title"],
            "artist": want.get("artist") or album["artist"],
            "primary_artist": want.get("artist") or album["artist"],
            "album_id": None, "release_date": None, "cover_url": None,
            "duration_ms": want.get("length_ms"),
        }
        tracks.append({**base,
                       "album": album["album"], "album_artist": album["artist"],
                       "track_no": want.get("number") or base.get("track_no"),
                       "disc_no": want.get("disc") or base.get("disc_no") or 1,
                       "album_total": total or base.get("album_total")})
    count = len(tracks)
    title = f"{album['artist']} - {album['album']} ({count} missing track{'s' if count != 1 else ''})"
    return "spotify", title, tracks
