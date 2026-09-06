"""Picks the YouTube Music recording that corresponds to a Spotify track.

Search alone is not enough. A query for a well known song returns live cuts,
covers, karaoke backings, sped-up edits and full album uploads, all of which
carry titles and artists that are nearly identical to the real thing. Two
signals separate them: the exact runtime, which Spotify gives us and which
differs sharply between a studio take and any re-performance of it, and the
presence of a version marker in the candidate title that the Spotify title
does not have.
"""

from __future__ import annotations

import re
import threading
from typing import Any

from rapidfuzz import fuzz
from ytmusicapi import YTMusic

# Words that mark a recording as something other than the album version. A
# candidate is penalised for each one it carries that the target lacks, so a
# genuine "- Live" track on Spotify still matches its live counterpart.
VERSION_MARKERS = (
    "live", "remix", "cover", "karaoke", "instrumental", "acoustic", "demo",
    "sped up", "spedup", "slowed", "reverb", "nightcore", "8d", "loop",
    "extended", "radio edit", "session", "unplugged", "tribute", "backing",
)

# Runtime difference, in seconds, past which a candidate scores nothing for
# duration. Studio versions of the same track rarely differ by more than a
# couple of seconds across services.
DURATION_TOLERANCE = 15.0

# Minimum total score required to accept a match. Below this the track is
# failed rather than downloaded, because a wrong file is more expensive to
# undo than a missing one once beets has imported it.
SCORE_FLOOR = 0.70

# Title and artist are necessary conditions, not tradeable ones. Without these
# gates a weighted sum lets one perfect signal mask a fatal weakness in
# another: a different song by the right artist at the right length, or a
# note-perfect cover by someone else, both clear the floor on total score
# alone. A candidate failing either gate is discarded outright.
TITLE_GATE = 0.60
ARTIST_GATE = 0.50

_FEAT = re.compile(r"\s*[\(\[]?\s*(feat|ft|featuring|with)\.?\s+[^\)\]]*[\)\]]?", re.I)
_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")

_local = threading.local()


class MatchError(Exception):
    """Raised when no candidate clears the confidence floor.

    A judgement about the results, not about reaching YouTube. The worker
    does not retry these, on the reasoning that an identical search returns
    identical results - which is true for a genuine miss and only for that.
    """


class SearchUnavailable(Exception):
    """YouTube Music could not be asked - rate limited, offline, changed.

    Kept apart from MatchError deliberately. Both used to be the same thing:
    every exception was swallowed and turned into "No results returned by
    YouTube Music", which the worker then refused to retry. One 429 and every
    remaining track in the job failed permanently, blaming the catalogue for
    a problem with the connection.
    """


def client() -> YTMusic:
    """One client per thread.

    ytmusicapi wraps a requests.Session, which is not thread-safe, and
    downloads run `concurrency` threads deep. A single shared client was
    borrowing one connection pool across all of them.
    """
    existing = getattr(_local, "client", None)
    if existing is None:
        existing = _local.client = YTMusic()
    return existing


def normalise(text: str) -> str:
    """Lowercase, drop featured-artist clauses and punctuation."""
    text = _FEAT.sub(" ", text or "")
    text = _PUNCT.sub(" ", text.lower())
    return _SPACE.sub(" ", text).strip()


def _markers(text: str) -> set[str]:
    lowered = (text or "").lower()
    return {marker for marker in VERSION_MARKERS if marker in lowered}


def _duration_score(candidate_seconds: int | None, target_ms: int | None) -> float:
    """1.0 for an exact runtime match, decaying to 0.0 at the tolerance."""
    if not candidate_seconds or not target_ms:
        return 0.5  # unknown: neither reward nor punish
    delta = abs(candidate_seconds - target_ms / 1000.0)
    return max(0.0, 1.0 - delta / DURATION_TOLERANCE)


def _candidate_artists(result: dict[str, Any]) -> str:
    return ", ".join(a.get("name", "") for a in (result.get("artists") or []))


def score(result: dict[str, Any], track: dict[str, Any]) -> tuple[float, dict[str, float]]:
    """Return an overall score in 0..1 plus its components, for logging."""
    target_title = normalise(track["title"])
    target_artist = normalise(track["artist"])
    cand_title = normalise(result.get("title", ""))
    cand_artist = normalise(_candidate_artists(result))

    # token_set_ratio ignores word order and extra words, so "Weird Fishes /
    # Arpeggi" still matches "Weird Fishes Arpeggi" and bracketed suffixes do
    # not wreck an otherwise correct title.
    title = fuzz.token_set_ratio(target_title, cand_title) / 100.0
    artist = fuzz.token_set_ratio(target_artist, cand_artist) / 100.0
    duration = _duration_score(result.get("duration_seconds"), track.get("duration_ms"))

    extra_markers = _markers(result.get("title", "")) - _markers(track["title"])
    penalty = 0.25 * len(extra_markers)

    bonus = 0.0
    if result.get("resultType") == "song":
        bonus += 0.04
    album = (result.get("album") or {}).get("name")
    if album and track.get("album") and normalise(album) == normalise(track["album"]):
        bonus += 0.06

    parts = {
        "title": title, "artist": artist, "duration": duration,
        "bonus": bonus, "penalty": penalty, "gated": 0.0,
    }
    if title < TITLE_GATE or artist < ARTIST_GATE:
        parts["gated"] = 1.0
        return 0.0, parts

    total = 0.40 * title + 0.25 * artist + 0.30 * duration + bonus - penalty
    # Deliberately not clamped at the top: bonuses can push a strong match past
    # 1.0, and that headroom keeps two near-identical candidates rankable.
    return max(0.0, total), parts


def _search(query: str, filter_: str | None, limit: int) -> list[dict[str, Any]]:
    """Ask YouTube Music, distinguishing "nothing found" from "could not ask".

    Every exception used to be swallowed into an empty list, so a rate limit
    was indistinguishable from an obscure B-side nobody has uploaded. The
    first is worth retrying in thirty seconds and the second never is.
    """
    try:
        return client().search(query=query, filter=filter_, limit=limit) or []
    except Exception as exc:
        raise SearchUnavailable(f"{type(exc).__name__}: {exc}"[:200]) from exc


def find(track: dict[str, Any]) -> tuple[str, float, dict[str, float]]:
    """Return (youtube url, score, components) for the best candidate.

    Songs are searched first because that catalogue carries real album
    metadata. Videos are only consulted when no song clears the floor, since
    they are far more likely to be uploads of the wrong thing.
    """
    query = f"{track['artist']} {track['title']}"
    candidates = [(c, "song") for c in _search(query, "songs", 8)]

    scored = [(*score(c, track), c) for c, _ in candidates]
    scored.sort(key=lambda row: row[0], reverse=True)

    if not scored or scored[0][0] < SCORE_FLOOR:
        # Videos carry weaker metadata, so they are discounted slightly to
        # keep a mediocre video from beating a decent song result.
        #
        # A failure here is not fatal when the song search already returned
        # something: this is a fallback, and losing real candidates because
        # the optional second query was rate limited would be the same
        # mistake in miniature.
        try:
            videos = _search(query, "videos", 5)
        except SearchUnavailable:
            if not scored:
                raise
            videos = []
        for video in videos:
            total, parts = score(video, track)
            scored.append((total * 0.95, parts, video))
        scored.sort(key=lambda row: row[0], reverse=True)

    if not scored:
        raise MatchError("No results returned by YouTube Music.")

    best_score, parts, best = scored[0]
    if best_score < SCORE_FLOOR:
        raise MatchError(
            f"Best candidate scored {best_score:.2f}, below the {SCORE_FLOOR:.2f} "
            f"floor (\u201c{best.get('title', '?')}\u201d)."
        )
    if not best.get("videoId"):
        raise MatchError("Best candidate has no playable video id.")

    return f"https://music.youtube.com/watch?v={best['videoId']}", best_score, parts
