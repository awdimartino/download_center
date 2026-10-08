"""Album-less tracks are not a split album (2M11).

A file with no album tag is its own record by design, with an album UUID of
its own, and is filed to Artist/Unknown Album/. Two of them in that folder
were counted as one album split in two, which failed Health for good: no
scan or fix could clear it.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from mutagen.easyid3 import EasyID3

from app import diskaudit, uuidtags

SILENCE = Path(__file__).parent / "fixtures" / "silence.mp3"


def _file(folder: Path, name: str, album_uuid: str, **tags: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    shutil.copy(SILENCE, path)
    audio = EasyID3(path)
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    uuidtags.write(path, str(uuid.uuid4()), album_uuid)


def test_two_album_less_tracks_in_one_folder_are_not_a_split_album(tmp_path):
    loose = tmp_path / "Aphex Twin" / "Unknown Album"
    _file(loose, "a.mp3", str(uuid.uuid4()), albumartist="Aphex Twin", title="A")
    _file(loose, "b.mp3", str(uuid.uuid4()), albumartist="Aphex Twin", title="B")

    assert diskaudit.run(tmp_path).split_albums == []


def test_an_album_whose_files_disagree_is_still_split(tmp_path):
    folder = tmp_path / "Boards" / "Geogaddi"
    _file(folder, "1.mp3", str(uuid.uuid4()), albumartist="Boards",
          album="Geogaddi", title="One")
    _file(folder, "2.mp3", str(uuid.uuid4()), albumartist="Boards",
          album="Geogaddi", title="Two")

    assert diskaudit.run(tmp_path).split_albums == [str(Path("Boards/Geogaddi"))]


def test_the_audit_records_each_folders_key_and_uuid(tmp_path):
    album_uuid = str(uuid.uuid4())
    folder = tmp_path / "Boards" / "Geogaddi"
    _file(folder, "1.mp3", album_uuid, albumartist="Boards", album="Geogaddi", title="One")

    keys = diskaudit.run(tmp_path).album_keys

    assert keys == {str(Path("Boards/Geogaddi")): ("boards\x1fgeogaddi", album_uuid)}
