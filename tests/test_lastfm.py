"""Matching Last.fm scrobbles to tracks in the library.

A backfill that quietly matched sixty per cent would poison every statistic
built on it afterwards and never say so, which is why the reporting is as
much the feature as the writing.
"""

from __future__ import annotations

import pytest

from app import lastfm


@pytest.mark.parametrize("a, b", [
    ("Radiohead", "radiohead"),
    ("Sigur Rós", "sigur rós"),
    ("Godspeed You! Black Emperor", "Godspeed You Black Emperor"),
    ("Anohni and the Johnsons", "Anohni & the Johnsons"),
])
def test_typography_does_not_stop_a_match(a, b):
    """A scrobble and a tag disagree about punctuation far more often than
    about the words."""
    assert lastfm.normalise(a) == lastfm.normalise(b)


def test_featured_artists_are_dropped():
    assert lastfm.normalise("Song (feat. Someone)") == lastfm.normalise("Song")
    assert lastfm.normalise("Song ft. Someone") == lastfm.normalise("Song")


def test_genuinely_different_titles_stay_different():
    assert lastfm.normalise("Let Down") != lastfm.normalise("Lucky")


def _index(pairs):
    return {(lastfm.normalise(a), lastfm.normalise(t)): u for a, t, u in pairs}


def test_a_clean_match_becomes_a_day_row():
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    # 2026-02-14 12:00 UTC
    planned = lastfm.plan("alex", "u-alex",
                          [("Radiohead", "Let Down", 1771070400)], index, None)
    assert planned["matched"] == 1
    assert planned["unmatched"] == 0
    assert len(planned["rows"]) == 1


def test_several_scrobbles_of_one_track_on_one_day_are_summed():
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    played = [("Radiohead", "Let Down", 1771070400 + n * 300) for n in range(3)]
    planned = lastfm.plan("alex", "u-alex", played, index, None)
    assert planned["matched"] == 3
    assert [row[2] for row in planned["rows"]] == [3]


def test_two_candidates_are_ambiguous_not_guessed():
    """A single and its album appearance are two files of one recording.
    Choosing by row order would be arbitrary and silent."""
    index = _index([("Radiohead", "Let Down", ["uuid-a", "uuid-b"])])
    planned = lastfm.plan("alex", "u-alex",
                          [("Radiohead", "Let Down", 1771070400)], index, None)
    assert planned["ambiguous"] == 1
    assert planned["matched"] == 0
    assert planned["rows"] == []
    assert planned["ambiguous_examples"]


def test_a_track_not_in_the_library_is_reported():
    planned = lastfm.plan("alex", "u-alex",
                          [("Some Band", "Some Song", 1771070400)], {}, None)
    assert planned["unmatched"] == 1
    assert planned["unmatched_examples"][0][0] == "Some Band - Some Song"


def test_scrobbles_from_the_snapshot_era_are_left_alone():
    """Snapshots already count these. Importing over the top would double
    every play on the handover day."""
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    played = [
        ("Radiohead", "Let Down", 1771070400),   # 2026-02-14
        ("Radiohead", "Let Down", 1773748800),   # 2026-03-17
    ]
    planned = lastfm.plan("alex", "u-alex", played, index, before="2026-03-01")

    assert planned["after_snapshots_began"] == 1
    assert planned["matched"] == 1
    assert all(row[0] < "2026-03-01" for row in planned["rows"])


def test_the_report_accounts_for_every_scrobble():
    """Matched + ambiguous + unmatched + overlapping must equal the total, or
    something was dropped without saying so."""
    index = _index([("A", "One", ["u1"]), ("B", "Two", ["u2", "u3"])])
    played = [
        ("A", "One", 1771070400),
        ("B", "Two", 1771070400),
        ("C", "Three", 1771070400),
        ("A", "One", 1773748800),
    ]
    planned = lastfm.plan("alex", "u-alex", played, index, before="2026-03-01")
    total = (planned["matched"] + planned["ambiguous"]
             + planned["unmatched"] + planned["after_snapshots_began"])
    assert total == planned["scrobbles"] == 4


@pytest.mark.parametrize("a, b", [
    ("Simon & Garfunkel", "Simon and Garfunkel"),
    ("Florence + the Machine", "Florence and the Machine"),
    ("Belle & Sebastian", "Belle and Sebastian"),
])
def test_ampersand_and_the_word_and_are_the_same_band(a, b):
    """One of the commonest differences between what a scrobbler sent and
    what the tag says. Stripping & as punctuation would leave the two
    spellings permanently unmatchable."""
    assert lastfm.normalise(a) == lastfm.normalise(b)
