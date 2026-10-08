"""The m4a repair tool leaves other files alone (2L14).

It moved each repaired file onto `<stem>.mp3`, which on Linux replaced an MP3
already there - the cross-format pair the filer's naming produces, each with
its own UUID, stars and plays. And it walked into the duplicates set aside.
"""

from __future__ import annotations

from tools import fix_broken_m4a as tool


def test_the_repaired_file_does_not_take_another_files_name(tmp_path):
    original = tmp_path / "01 - Song.m4a"
    original.write_bytes(b"m4a")
    (tmp_path / "01 - Song.mp3").write_bytes(b"the mp3 already here")

    final = tool.free_name(original, original.with_suffix(".mp3"))

    assert final.name == "01 - Song (2).mp3"


def test_a_remux_keeps_its_own_name(tmp_path):
    original = tmp_path / "01 - Song.m4a"
    original.write_bytes(b"m4a")
    assert tool.free_name(original, original) == original


def test_the_duplicates_set_aside_are_not_walked(tmp_path):
    live = tmp_path / "A" / "B" / "01.m4a"
    aside = tmp_path / tool.QUARANTINE_NAME / "A" / "B" / "01.m4a"
    for path in (live, aside):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not an mp4")

    assert tool.find_targets([tmp_path], every=True) == [live]
