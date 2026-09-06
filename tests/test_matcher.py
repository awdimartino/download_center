"""Picking a recording, and telling "not found" apart from "could not ask".

The distinction is the whole point of this file. The worker deliberately
never retries a MatchError, because an identical search returns identical
results. That reasoning is correct for a genuine miss and wrong for a rate
limit - and every exception used to be swallowed into the former, so one 429
failed every remaining track in a job, permanently, blaming YouTube's
catalogue for a problem with the connection.
"""

from __future__ import annotations

import threading

import pytest

from app import matcher


def _track(title="Airbag", artist="Radiohead", ms=284_000, album="OK Computer"):
    return {"title": title, "artist": artist, "duration_ms": ms, "album": album}


def _result(title="Airbag", artists=("Radiohead",), seconds=284,
            kind="song", video="abc123", album="OK Computer"):
    return {
        "title": title,
        "artists": [{"name": a} for a in artists],
        "duration_seconds": seconds,
        "resultType": kind,
        "videoId": video,
        "album": {"name": album},
    }


# --- the two failure modes ------------------------------------------------

def test_a_rate_limit_is_not_reported_as_no_results(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("HTTP 429 Too Many Requests")

    monkeypatch.setattr(matcher, "client", lambda: type("C", (), {"search": boom})())

    with pytest.raises(matcher.SearchUnavailable, match="429"):
        matcher.find(_track())


def test_a_genuine_miss_is_a_match_error(monkeypatch):
    monkeypatch.setattr(matcher, "_search", lambda *a, **k: [])

    with pytest.raises(matcher.MatchError):
        matcher.find(_track())


def test_the_two_are_not_the_same_class():
    """The worker branches on this, so it has to stay true."""
    assert not issubclass(matcher.SearchUnavailable, matcher.MatchError)
    assert not issubclass(matcher.MatchError, matcher.SearchUnavailable)


def test_a_failed_video_fallback_does_not_discard_good_song_results(monkeypatch):
    """The video search is a fallback. Losing real candidates because the
    optional second query was rate limited is the same mistake in miniature."""
    calls = []

    def search(query, filter_, limit):
        calls.append(filter_)
        if filter_ == "songs":
            # Deliberately mediocre, so the video fallback is reached.
            return [_result(title="Airbag (Live)", seconds=290)]
        raise matcher.SearchUnavailable("429")

    monkeypatch.setattr(matcher, "_search", search)

    # Scores below the floor because of the "live" marker, so it still
    # raises - but as a MatchError about the results, not a transport error.
    with pytest.raises(matcher.MatchError):
        matcher.find(_track())
    assert calls == ["songs", "videos"]


def test_a_failed_song_search_still_propagates(monkeypatch):
    def search(query, filter_, limit):
        raise matcher.SearchUnavailable("429")

    monkeypatch.setattr(matcher, "_search", search)
    with pytest.raises(matcher.SearchUnavailable):
        matcher.find(_track())


# --- the client -------------------------------------------------------------

def test_each_thread_gets_its_own_client(monkeypatch):
    """ytmusicapi wraps a requests.Session, which is not thread-safe, and
    downloads run `concurrency` threads deep."""
    made = []

    class FakeYTMusic:
        def __init__(self):
            made.append(threading.current_thread().name)

    monkeypatch.setattr(matcher, "YTMusic", FakeYTMusic)
    monkeypatch.setattr(matcher, "_local", threading.local())

    # Real references, not ids: CPython reuses an id once an object is
    # collected, which made an earlier version of this assertion nonsense.
    seen: dict[str, list] = {}

    def grab():
        name = threading.current_thread().name
        seen[name] = [matcher.client(), matcher.client()]

    threads = [threading.Thread(target=grab) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(made) == 3, "one client per thread, not one shared"
    # Two calls on one thread return the same object...
    for pair in seen.values():
        assert pair[0] is pair[1]
    # ...and no two threads share one.
    firsts = [pair[0] for pair in seen.values()]
    assert len({id(c) for c in firsts}) == 3


# --- scoring, which the gates protect ---------------------------------------

def test_a_good_match_is_found(monkeypatch):
    monkeypatch.setattr(matcher, "_search",
                        lambda q, f, l: [_result()] if f == "songs" else [])
    url, score, parts = matcher.find(_track())
    assert "abc123" in url
    assert score >= matcher.SCORE_FLOOR


def test_a_cover_by_someone_else_is_gated_out():
    """A note-perfect cover clears a weighted sum on title and duration
    alone, which is why artist is a gate rather than a weight."""
    total, parts = matcher.score(
        _result(artists=("Some Other Band",)), _track())
    assert parts["gated"] == 1.0
    assert total == 0.0


def test_a_different_song_by_the_right_artist_is_gated_out():
    total, parts = matcher.score(_result(title="Karma Police"), _track())
    assert parts["gated"] == 1.0


def test_a_live_version_is_penalised_when_the_target_is_not_live():
    studio, _ = matcher.score(_result(), _track())
    live, _ = matcher.score(_result(title="Airbag (Live)"), _track())
    assert live < studio


def test_a_live_target_is_not_penalised_for_matching_a_live_take():
    """Penalising markers the target also has would make a genuine live
    track unmatchable."""
    _, parts = matcher.score(_result(title="Airbag (Live)"),
                             _track(title="Airbag (Live)"))
    assert parts["penalty"] == 0.0


def test_an_unknown_duration_neither_rewards_nor_punishes():
    assert matcher._duration_score(None, 284_000) == 0.5
    assert matcher._duration_score(284, None) == 0.5


def test_duration_scoring_decays_to_zero_at_the_tolerance():
    assert matcher._duration_score(284, 284_000) == 1.0
    assert matcher._duration_score(284 + int(matcher.DURATION_TOLERANCE),
                                   284_000) == 0.0
