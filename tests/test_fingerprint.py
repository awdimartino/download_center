"""tools/fingerprint.py, the AcoustID backfill (CODE_REVIEW L34).

It wrote the MusicBrainz recording id into the "Acoustid Id" frame, never
checkpointed a file it could not match - so every rerun paid for it again -
and a --from-list reached into the quarantine. AcoustID and fpcalc are faked.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from mutagen.id3 import ID3, TXXX, UFID

from tools import fingerprint

SILENCE = Path(__file__).parent / "fixtures" / "silence.mp3"
ROOT = Path(__file__).resolve().parent.parent


class Answer:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def test_the_acoustid_frame_holds_acoustids_id_not_the_recordings(tmp_path,
                                                                 monkeypatch):
    payload = {"status": "ok", "results": [{
        "id": "acoustid-track-1", "score": 0.97,
        "recordings": [{"id": "mb-recording-1", "title": "Song",
                        "artists": [{"name": "Artist"}]}]}]}
    monkeypatch.setattr(fingerprint.requests, "post",
                        lambda *a, **k: Answer(payload))
    match = fingerprint.lookup("key", 200, "fp")
    path = tmp_path / "a.mp3"
    shutil.copy(SILENCE, path)

    fingerprint.write_ids(path, match["recording_id"], match["acoustid_id"])

    tags = ID3(path)
    assert tags.getall("TXXX:Acoustid Id")[0].text == ["acoustid-track-1"]
    assert tags.getall("UFID:http://musicbrainz.org")[0].data == b"mb-recording-1"


def test_an_answer_with_nothing_in_it_is_a_no_not_a_failure(monkeypatch):
    monkeypatch.setattr(fingerprint.requests, "post",
                        lambda *a, **k: Answer({"status": "ok", "results": []}))
    assert fingerprint.lookup("key", 200, "fp") == {"score": 0.0}


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["fingerprint", *map(str, argv)])
    monkeypatch.setattr(fingerprint, "wait_for_quiet", lambda *a, **k: None)
    monkeypatch.setattr(fingerprint.time, "sleep", lambda s: None)
    return fingerprint.main()


def test_a_file_with_no_match_is_not_asked_about_again(tmp_path, monkeypatch):
    music = tmp_path / "music"
    music.mkdir()
    shutil.copy(SILENCE, music / "a.mp3")
    checkpoint = tmp_path / "progress.tsv"
    asked = []
    monkeypatch.setattr(fingerprint, "fingerprint", lambda path: (200, "fp"))
    monkeypatch.setattr(fingerprint, "lookup",
                        lambda key, d, fp: asked.append(1) or {"score": 0.0})

    _run(monkeypatch, music, "--api-key", "k", "--apply", "--checkpoint", checkpoint)
    _run(monkeypatch, music, "--api-key", "k", "--apply", "--checkpoint", checkpoint)
    assert len(asked) == 1

    _run(monkeypatch, music, "--api-key", "k", "--apply", "--checkpoint", checkpoint,
         "--retry-failed")
    assert len(asked) == 2


def test_a_lookup_that_never_answered_is_asked_again(tmp_path, monkeypatch):
    music = tmp_path / "music"
    music.mkdir()
    shutil.copy(SILENCE, music / "a.mp3")
    checkpoint = tmp_path / "progress.tsv"
    asked = []
    monkeypatch.setattr(fingerprint, "fingerprint", lambda path: (200, "fp"))
    monkeypatch.setattr(fingerprint, "lookup",
                        lambda key, d, fp: asked.append(1) or None)

    _run(monkeypatch, music, "--api-key", "k", "--apply", "--checkpoint", checkpoint)
    _run(monkeypatch, music, "--api-key", "k", "--apply", "--checkpoint", checkpoint)
    assert len(asked) == 2


def test_a_list_of_files_never_reaches_into_the_quarantine(tmp_path, monkeypatch):
    kept = tmp_path / "music" / "a.mp3"
    buried = tmp_path / "music" / fingerprint.QUARANTINE_NAME / "b.mp3"
    for path in (kept, buried):
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SILENCE, path)
    listing = tmp_path / "list.txt"
    listing.write_text(f"{kept}\n{buried}\n", encoding="utf-8")
    seen = []
    monkeypatch.setattr(fingerprint, "fingerprint",
                        lambda path: seen.append(path) or None)

    _run(monkeypatch, tmp_path, "--api-key", "k", "--from-list", listing,
         "--checkpoint", tmp_path / "progress.tsv")

    assert seen == [kept]


def test_an_earlier_runs_misfiled_frame_is_removed(tmp_path):
    path = tmp_path / "a.mp3"
    shutil.copy(SILENCE, path)
    tags = ID3()
    tags.add(UFID(owner="http://musicbrainz.org", data=b"mb-recording-1"))
    tags.add(TXXX(encoding=3, desc="Acoustid Id", text="mb-recording-1"))
    tags.save(path)

    assert fingerprint.clear_misfiled_acoustid(path) is True
    assert not ID3(path).getall("TXXX:Acoustid Id")
    assert ID3(path).getall("UFID:http://musicbrainz.org")


def test_a_real_acoustid_frame_is_left_alone(tmp_path):
    path = tmp_path / "a.mp3"
    shutil.copy(SILENCE, path)
    tags = ID3()
    tags.add(UFID(owner="http://musicbrainz.org", data=b"mb-recording-1"))
    tags.add(TXXX(encoding=3, desc="Acoustid Id", text="acoustid-track-1"))
    tags.save(path)

    assert fingerprint.clear_misfiled_acoustid(path) is False


def test_the_tools_are_in_the_image_their_usage_runs_them_from():
    assert "COPY tools ./tools" in (ROOT / "Dockerfile").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_nice(monkeypatch):
    monkeypatch.setattr(fingerprint.os, "nice", lambda n: 0, raising=False)
