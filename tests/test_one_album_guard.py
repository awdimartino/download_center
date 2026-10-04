"""A folder holding two albums is refused folder-wide actions (CODE_REVIEW H9).

Album actions treat a folder as one record. Older folders can hold two, and
two names can sanitise to one folder. Renaming one silently absorbed the
second album and orphaned its registry row.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from test_filer import album_on_disk, space  # noqa: F401
from app import filer, main, registry, replaygain, uuidtags


def _two_albums_in_one_folder(space):
    first = album_on_disk(space, "Aiden Williams", "Believe", ["One", "Two"])
    second = album_on_disk(space, "Aiden Williams", "Breakup", ["Three"])
    folder = first[0].path.parent
    moved = folder / second[0].path.name
    second[0].path.rename(moved)
    second[0].path.parent.rmdir()
    return folder, first, moved


def test_renaming_a_folder_holding_two_albums_is_refused(space):
    folder, first, stray = _two_albums_in_one_folder(space)
    stray_uuid = uuidtags.read(stray)[1]

    with pytest.raises(filer.NotEditable, match="more than one album"):
        filer.retag_album(space, folder, albumartist="Aiden Williams",
                          album="Believe (Deluxe)")

    assert filer.read_meta(stray).album == "Breakup"
    assert uuidtags.read(stray)[1] == stray_uuid
    assert registry.known(space.library_id,
                          registry.album_key("Aiden Williams", "Breakup")) == stray_uuid


def test_one_album_with_an_untagged_stray_is_still_one_album(space, tmp_path):
    from test_filer import track
    import shutil

    first = album_on_disk(space, "Artist", "Record", ["One"])
    folder = first[0].path.parent
    shutil.copy(track(tmp_path, name="s.mp3", title="Stray"), folder / "s.mp3")

    filer.require_one_album(folder)


@pytest.mark.asyncio
async def test_matching_a_folder_holding_two_albums_is_refused(space, monkeypatch):
    from types import SimpleNamespace

    folder, _, _ = _two_albums_in_one_folder(space)
    monkeypatch.setattr(main.workspace, "for_session", lambda identity, lid: space)
    monkeypatch.setattr(main.library, "album_dir",
                        lambda identity, lid, name: folder)
    session = SimpleNamespace(identity=SimpleNamespace(username="alex"))

    with pytest.raises(HTTPException) as refused:
        await main.library_match(
            main.AlbumTarget(library_id=1, folder="x"), session)
    assert refused.value.status_code == 409


def test_replaygain_skips_a_folder_holding_two_albums(space, identity, monkeypatch):
    folder, _, _ = _two_albums_in_one_folder(space)
    measured = []
    monkeypatch.setattr(replaygain.library, "album_dir",
                        lambda identity, lid, name: folder)
    monkeypatch.setattr(replaygain.inbox, "settled", lambda path: True)
    monkeypatch.setattr(replaygain, "measure", measured.append)
    replaygain.operations.reset()

    result = replaygain.measure_all(identity, [(1, "Aiden Williams/Believe")])

    assert measured == []
    assert result["skipped"] == ["Aiden Williams/Believe: holds more than one album"]
