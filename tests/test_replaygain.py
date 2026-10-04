"""Measuring ReplayGain across many albums.

The scanner itself is rsgain and is not run here - it was checked against
copies of real files on the Pi (both UUID tags survive on MP3, M4A and FLAC).
What is tested is the loop around it: a run of thousands of tracks has to be
stoppable, has to carry on past one bad album, and must not report an album
it never touched as measured.
"""

from __future__ import annotations

import subprocess

import pytest

from app import navidrome, operations, replaygain


@pytest.fixture
def albums(tmp_path, identity, monkeypatch):
    root = tmp_path / "music"
    for name in ("A/One", "A/Two", "A/Three"):
        (root / name).mkdir(parents=True)
    monkeypatch.setattr(replaygain.inbox, "receiving", lambda path: False)
    monkeypatch.setattr(navidrome, "notify", lambda: True)
    operations.reset()
    yield [(1, "A/One"), (1, "A/Two"), (1, "A/Three")]
    operations.reset()


def test_every_album_is_measured(albums, identity, monkeypatch):
    seen = []
    monkeypatch.setattr(replaygain, "measure", lambda path: seen.append(path.name))

    result = replaygain.measure_all(identity, albums)

    assert seen == ["One", "Two", "Three"]
    assert result["measured"] == 3 and result["failures"] == 0


def test_one_failure_does_not_end_the_run(albums, identity, monkeypatch):
    def measure(path):
        if path.name == "Two":
            raise RuntimeError("rsgain failed on Two: bad frame")
    monkeypatch.setattr(replaygain, "measure", measure)

    result = replaygain.measure_all(identity, albums)

    assert result["measured"] == 2
    assert result["failed"] == ["rsgain failed on Two: bad frame"]


def test_a_timeout_is_a_failure_not_a_crash(albums, identity, monkeypatch):
    def measure(path):
        raise subprocess.TimeoutExpired("rsgain", 1)
    monkeypatch.setattr(replaygain, "measure", measure)

    assert replaygain.measure_all(identity, albums)["failures"] == 3


def test_a_stop_is_honoured_between_albums(albums, identity, monkeypatch):
    seen = []

    def measure(path):
        seen.append(path.name)
        operations.get(replaygain.NAME, "alex").stop_requested = True
    monkeypatch.setattr(replaygain, "measure", measure)

    result = replaygain.measure_all(identity, albums)

    assert seen == ["One"]
    assert result["stopped"] is True and result["measured"] == 1


def test_an_album_that_moved_is_skipped_not_counted(albums, identity,
                                                    monkeypatch):
    monkeypatch.setattr(replaygain, "measure", lambda path: None)

    result = replaygain.measure_all(identity, albums + [(1, "A/Gone")])

    assert result["measured"] == 3
    assert len(result["skipped"]) == 1


def test_an_album_still_arriving_is_left_alone(albums, identity, monkeypatch):
    """rsgain rewrites the file; the filer may be about to move it."""
    monkeypatch.setattr(replaygain.inbox, "receiving",
                        lambda path: path.name == "Two")
    seen = []
    monkeypatch.setattr(replaygain, "measure", lambda path: seen.append(path.name))

    replaygain.measure_all(identity, albums)

    assert seen == ["One", "Three"]


def test_progress_is_reported(albums, identity, monkeypatch):
    monkeypatch.setattr(replaygain, "measure", lambda path: None)

    replaygain.measure_all(identity, albums)

    assert operations.get(replaygain.NAME, "alex").progress == {
        "done": 2, "total": 3, "album": "A/Three"}


def test_without_rsgain_it_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(replaygain.shutil, "which", lambda name: None)
    with pytest.raises(replaygain.Unavailable):
        replaygain.measure(tmp_path)
