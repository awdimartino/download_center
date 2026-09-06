"""Naming files, and getting them into place without losing one."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import staging


# --- sanitising -------------------------------------------------------------

def test_a_backslash_is_stripped():
    """It was not. Inside the character class `\\|` escaped the pipe, so the
    backslash itself never joined the set - and a title like "AC\\DC" came
    through untouched, to be read as a directory separator on Windows."""
    assert "\\" not in staging.sanitize("AC\\DC - Back In Black")


@pytest.mark.parametrize("char", list('<>:"/\\|?*'))
def test_every_character_windows_forbids_is_stripped(char):
    assert char not in staging.sanitize(f"a{char}b")


def test_control_characters_are_stripped():
    assert staging.sanitize("a\x00\x1fb") == "a__b"


def test_brackets_survive_because_they_are_legal():
    """Sanitising these would mangle "Song [Remix]", which is a real title on
    both filesystems. The glob that could not cope with them was the bug."""
    assert staging.sanitize("Song [Remix]") == "Song [Remix]"


def test_a_reserved_device_name_is_prefixed():
    assert staging.sanitize("CON").startswith("_")
    assert staging.sanitize("com1.mp3").startswith("_")


def test_a_trailing_dot_or_space_is_removed():
    assert staging.sanitize("Album.") == "Album"
    assert staging.sanitize("Album ") == "Album"


def test_a_long_name_is_truncated_without_a_trailing_dot():
    got = staging.sanitize("x" * 200 + ".")
    assert len(got) <= staging.MAX_COMPONENT
    assert not got.endswith((".", " "))


def test_an_empty_name_still_produces_something_usable():
    assert staging.sanitize("") == "unknown"
    assert staging.sanitize(None) == "unknown"
    # Not "unknown": every character was illegal rather than absent, so it
    # becomes "___" - still a legal single component, which is the contract.
    assert staging.sanitize("///") == "___"


def test_a_sanitised_name_is_one_path_component():
    """The whole point: this value is joined onto a directory."""
    for awkward in ("AC\\DC", "a/b", "x:y", "p|q"):
        assert len(Path(staging.sanitize(awkward)).parts) == 1


# --- moving into place ------------------------------------------------------

def test_a_collision_is_numbered_rather_than_overwritten(tmp_path):
    target = tmp_path / "Song.mp3"
    target.write_bytes(b"original")
    source = tmp_path / "incoming.mp3"
    source.write_bytes(b"new")

    landed = staging._move_into_place(source, target)

    assert target.read_bytes() == b"original"
    assert landed.name == "Song (2).mp3"
    assert landed.read_bytes() == b"new"


def test_running_out_of_names_raises_rather_than_overwriting(tmp_path):
    """The loop used to leave `target` at the original name when all 98 were
    taken, so the one case the numbering exists for ended in os.replace
    destroying the file it was protecting."""
    target = tmp_path / "Song.mp3"
    target.write_bytes(b"original")
    for n in range(2, 100):
        (tmp_path / f"Song ({n}).mp3").write_bytes(b"taken")
    source = tmp_path / "incoming.mp3"
    source.write_bytes(b"new")

    with pytest.raises(FileExistsError):
        staging._move_into_place(source, target)

    assert target.read_bytes() == b"original"
    assert source.is_file(), "a refused move must leave the source alone"


def test_a_move_creates_the_parent_directory(tmp_path):
    source = tmp_path / "incoming.mp3"
    source.write_bytes(b"new")
    target = tmp_path / "albums" / "Artist - Album" / "01 Song.mp3"

    landed = staging._move_into_place(source, target)
    assert landed == target
    assert target.is_file()


# --- naming -----------------------------------------------------------------

def test_a_multi_disc_track_carries_its_disc_number():
    item = {"title": "Song", "artist": "A", "track_no": 4, "disc_no": 2}
    assert staging.track_filename(item, multi_disc=True).startswith("2-04 ")
    assert staging.track_filename(item, multi_disc=False).startswith("04 ")


def test_a_track_with_no_number_still_produces_a_filename():
    item = {"title": "Song", "artist": "A", "track_no": None, "disc_no": None}
    assert staging.track_filename(item, multi_disc=False).endswith(".mp3")
