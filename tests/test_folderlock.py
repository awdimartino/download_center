"""One change at a time per album folder (CODE_REVIEW M14).

Rename, combine, covers, match and ReplayGain ran in threads with nothing
stopping two of them meeting on one folder; a ReplayGain run lasts hours.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import folderlock, replaygain
from test_filer import album_on_disk, space  # noqa: F401
from app import library
from app import workspace
from app.api import library_edit as library_edit_routes


def test_a_held_folder_refuses_a_second_change(tmp_path):
    with folderlock.holding(tmp_path / "A"):
        with pytest.raises(folderlock.Busy):
            with folderlock.holding(tmp_path / "A"):
                pass


def test_a_folder_overlaps_its_parents_and_children(tmp_path):
    with folderlock.holding(tmp_path / "Artist" / "Album"):
        with pytest.raises(folderlock.Busy):
            with folderlock.holding(tmp_path / "Artist"):
                pass
        with pytest.raises(folderlock.Busy):
            with folderlock.holding(tmp_path / "Artist" / "Album" / "CD1"):
                pass
        with folderlock.holding(tmp_path / "Artist" / "Other"):
            pass


def test_a_folder_is_free_again_afterwards_even_after_a_failure(tmp_path):
    with pytest.raises(RuntimeError):
        with folderlock.holding(tmp_path / "A"):
            raise RuntimeError("boom")
    with folderlock.holding(tmp_path / "A"):
        pass


def test_replaygain_skips_a_folder_being_changed(tmp_path, identity, monkeypatch):
    folder = tmp_path / "A" / "B"
    folder.mkdir(parents=True)
    monkeypatch.setattr(replaygain.library, "album_dir",
                        lambda identity, lid, name, **_: folder)
    monkeypatch.setattr(replaygain.inbox, "receiving", lambda path: False)
    measured = []
    monkeypatch.setattr(replaygain, "measure", measured.append)
    replaygain.operations.reset()

    with folderlock.holding(folder):
        result = replaygain.measure_all(identity, [(1, "A/B")])

    assert measured == []
    assert result["skipped"] == ["A/B: being changed by something else"]


@pytest.mark.asyncio
async def test_an_album_edit_during_another_change_is_a_409(space, monkeypatch):
    filed = album_on_disk(space, "Artist", "Album", ["One"])
    folder = filed[0].path.parent
    monkeypatch.setattr(workspace, "for_session", lambda identity, lid: space)
    monkeypatch.setattr(library, "album_dir",
                        lambda identity, lid, name, **_: folder)
    session = SimpleNamespace(identity=SimpleNamespace(username="alex"))

    with folderlock.holding(folder):
        with pytest.raises(HTTPException) as refused:
            await library_edit_routes.library_album_edit(
                library_edit_routes.AlbumEdit(library_id=1, folder="Artist/Album",
                               album_artist="Artist", album="Renamed"), session)

    assert refused.value.status_code == 409
