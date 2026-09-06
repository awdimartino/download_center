"""Matching Last.fm scrobbles to tracks in the library.

A backfill that quietly matched sixty per cent would poison every statistic
built on it afterwards and never say so, which is why the reporting is as
much the feature as the writing.
"""

from __future__ import annotations

import sys

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


# --- surviving a flaky API --------------------------------------------------

def test_a_server_error_is_retried(monkeypatch):
    """One 500 part way through a quarter of an hour of requests threw away
    every page already fetched. Same mistake as treating a rate-limited
    search as "no results", and more expensive."""
    import urllib.error
    calls = {"n": 0}

    def flaky(request, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(
                "u", 500, "Internal Server Error", {}, None)
        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"ok": true}'
        return Response()

    monkeypatch.setattr(lastfm.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(lastfm.time, "sleep", lambda s: None)
    monkeypatch.setattr(lastfm.json, "load", lambda r: {"ok": True})

    assert lastfm._call("user.getRecentTracks", api_key="k") == {"ok": True}
    assert calls["n"] == 3


def test_a_client_error_is_not_retried(monkeypatch):
    """A 4xx means the request was wrong. Repeating it cannot help."""
    import urllib.error
    calls = {"n": 0}

    def refused(request, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    monkeypatch.setattr(lastfm.urllib.request, "urlopen", refused)
    monkeypatch.setattr(lastfm.time, "sleep", lambda s: None)

    with pytest.raises(lastfm.LastfmError, match="403"):
        lastfm._call("user.getRecentTracks", api_key="k")
    assert calls["n"] == 1


def test_an_error_in_the_body_is_not_retried(monkeypatch):
    """Last.fm answering "invalid parameters" is not a transient failure."""
    calls = {"n": 0}

    def answered(request, timeout=None):
        calls["n"] += 1
        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return Response()

    monkeypatch.setattr(lastfm.urllib.request, "urlopen", answered)
    monkeypatch.setattr(lastfm.time, "sleep", lambda s: None)
    monkeypatch.setattr(lastfm.json, "load",
                        lambda r: {"error": 6, "message": "No user"})

    with pytest.raises(lastfm.LastfmError, match="No user"):
        lastfm._call("user.getInfo", api_key="k")
    assert calls["n"] == 1


def test_it_gives_up_eventually(monkeypatch):
    import urllib.error

    def always_500(request, timeout=None):
        raise urllib.error.HTTPError("u", 503, "Unavailable", {}, None)

    monkeypatch.setattr(lastfm.urllib.request, "urlopen", always_500)
    monkeypatch.setattr(lastfm.time, "sleep", lambda s: None)

    with pytest.raises(lastfm.LastfmError, match="attempts"):
        lastfm._call("user.getRecentTracks", api_key="k")


def test_rows_are_labelled_with_the_navidrome_name(monkeypatch):
    """A snapshot row saying "alex" beside an imported row saying
    "argyle_nz" for the same user_id reads like two different listeners."""
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    planned = lastfm.plan("alex", "u-alex",
                          [("Radiohead", "Let Down", 1771070400)], index, None)
    assert planned["username"] == "alex"


# --- auditing the misses ----------------------------------------------------

def test_a_near_miss_is_surfaced_with_its_score():
    """"Not in the library" is a claim that has to be checkable. A high score
    means the matcher missed something it should have caught; a low one means
    the track really is absent."""
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    planned = lastfm.plan("alex", "u-alex",
                          [("Radiohead", "Let Down (Remastered)", 1771070400)],
                          index, None)
    assert planned["unmatched"] == 1

    audit = lastfm.audit_unmatched(planned, index)
    assert len(audit) == 1
    assert audit[0]["scrobbled"] == "Radiohead - Let Down (Remastered)"
    # Shown normalised: that is what the matcher actually compared, which is
    # the useful thing to see when judging why a match was missed.
    assert "let down" in audit[0]["nearest"]
    assert audit[0]["score"] >= 85, "a remaster suffix should read as close"


def test_a_genuine_absence_scores_low():
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    planned = lastfm.plan("alex", "u-alex",
                          [("Some Other Band", "Unrelated Song", 1771070400)],
                          index, None)
    audit = lastfm.audit_unmatched(planned, index)
    assert audit[0]["score"] < 85


def test_the_audit_is_ordered_by_how_often_it_was_scrobbled():
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    played = ([("Band A", "Rare", 1771070400)]
              + [("Band B", "Often", 1771070400 + n) for n in range(5)])
    planned = lastfm.plan("alex", "u-alex", played, index, None)
    audit = lastfm.audit_unmatched(planned, index)
    assert audit[0]["scrobbled"] == "Band B - Often"
    assert audit[0]["scrobbles"] == 5


def test_the_audit_flag_is_actually_wired_up(tmp_path, monkeypatch, capsys):
    """The flag existed and did nothing for one deploy: argparse knew it,
    main() never called audit_unmatched, and the tests above passed because
    they exercise the function rather than the command. A flag that is
    silently ignored is the exact failure this codebase keeps producing."""
    import app.lastfm as lf

    target = tmp_path / "unmatched.txt"
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    monkeypatch.setattr(lf, "_credentials", lambda user: ("k", "s", "sk"))
    monkeypatch.setattr(lf, "username_for", lambda *a: "argyle_nz")
    monkeypatch.setattr(lf, "library_index", lambda c: index)
    monkeypatch.setattr(lf, "scrobbles",
                        lambda *a, **k: [("Nobody", "Nothing", 1771070400)])
    monkeypatch.setattr(lf.ledger, "connect", lambda p: None)

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args=()):
            class R:
                def fetchone(self_inner): return ("u-alex",)
            return R()
    monkeypatch.setattr(lf.navidrome, "open_db", lambda: FakeConn())
    monkeypatch.setattr(lf.ledger, "connection", lambda: FakeConn())
    monkeypatch.setattr(sys, "argv",
                        ["lastfm", "alex", "--audit", str(target)])

    assert lf.main() == 0
    assert target.exists(), "--audit was accepted and did nothing"
    body = target.read_text(encoding="utf-8")
    assert "Nobody - Nothing" in body
    assert "wrote 1 unmatched titles" in capsys.readouterr().out
