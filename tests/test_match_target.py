"""A candidate list belongs to the album it was asked about (CODE_REVIEW C3).

Candidate lookups run one at a time for the whole server. Asking about a
second album while the first was in flight used to hand back the first
one's operation, with nothing saying which album it was about; the page drew
its answer under the second album, and *Use this* retagged the second album
as the first one's release - fusing the two.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import beets_runner, inbox, library, main, operations, workspace


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    operations.reset()
    operations._on_change = None
    main._offered.clear()
    monkeypatch.setattr(workspace, "for_session", lambda identity, lid: object())
    monkeypatch.setattr(library, "album_dir",
                        lambda identity, lid, folder: tmp_path / folder)
    monkeypatch.setattr(inbox, "receiving", lambda path: False)
    yield
    operations.reset()


def _session(username="alex"):
    return SimpleNamespace(identity=SimpleNamespace(username=username))


async def _settle(operation):
    if operation.task:
        await operation.task


@pytest.mark.asyncio
async def test_the_answer_names_the_album_it_is_about(monkeypatch):
    monkeypatch.setattr(beets_runner, "candidates",
                        lambda space, path: {"candidates": [{"id": "mb-a"}]})
    answer = await main.library_match(
        main.AlbumTarget(library_id=1, folder="Artist/A"), _session())
    operation = operations.get("candidates", "alex")
    await _settle(operation)

    assert answer["operation"]["target"] == {"library_id": 1, "folder": "Artist/A"}
    assert operation.result["library_id"] == 1
    assert operation.result["folder"] == "Artist/A"


@pytest.mark.asyncio
async def test_a_lookup_refused_while_another_runs_says_whose_it_is(monkeypatch):
    release = threading.Event()

    def slow(space, path):
        release.wait(timeout=5)
        return {"candidates": [{"id": "mb-a"}]}

    monkeypatch.setattr(beets_runner, "candidates", slow)
    await main.library_match(
        main.AlbumTarget(library_id=1, folder="Artist/A"), _session())
    second = await main.library_match(
        main.AlbumTarget(library_id=1, folder="Artist/B"), _session())

    assert second["started"] is False
    assert second["operation"]["target"]["folder"] == "Artist/A"
    release.set()
    await _settle(operations.get("candidates", "alex"))


@pytest.mark.asyncio
async def test_a_release_offered_for_one_album_cannot_be_applied_to_another(
        monkeypatch):
    monkeypatch.setattr(beets_runner, "candidates",
                        lambda space, path: {"candidates": [{"id": "mb-a"}]})
    await main.library_match(
        main.AlbumTarget(library_id=1, folder="Artist/A"), _session())
    await _settle(operations.get("candidates", "alex"))

    with pytest.raises(HTTPException) as refused:
        await main.library_match_apply(
            main.AlbumChoice(library_id=1, folder="Artist/B", release_id="mb-a"),
            _session())
    assert refused.value.status_code == 409
    assert operations.get("import", "alex").status == operations.IDLE


@pytest.mark.asyncio
async def test_a_release_offered_to_someone_else_is_not_offered_to_you(monkeypatch):
    monkeypatch.setattr(beets_runner, "candidates",
                        lambda space, path: {"candidates": [{"id": "mb-a"}]})
    await main.library_match(
        main.AlbumTarget(library_id=1, folder="Artist/A"), _session("kelly"))
    await _settle(operations.get("candidates", "kelly"))

    with pytest.raises(HTTPException):
        await main.library_match_apply(
            main.AlbumChoice(library_id=1, folder="Artist/A", release_id="mb-a"),
            _session("alex"))


@pytest.mark.asyncio
async def test_a_release_offered_for_this_album_is_applied(monkeypatch):
    monkeypatch.setattr(beets_runner, "candidates",
                        lambda space, path: {"candidates": [{"id": "mb-a"}]})
    applied = []
    monkeypatch.setattr(main.filer, "album_key_of", lambda path: None)
    monkeypatch.setattr(main, "_before_edit", lambda *args: [])
    monkeypatch.setattr(
        beets_runner, "import_chosen",
        lambda space, path, release: applied.append((Path(path).name, release))
        or {"imported": 0})
    await main.library_match(
        main.AlbumTarget(library_id=1, folder="Artist/A"), _session())
    await _settle(operations.get("candidates", "alex"))

    answer = await main.library_match_apply(
        main.AlbumChoice(library_id=1, folder="Artist/A", release_id="mb-a"),
        _session())
    await _settle(operations.get("import", "alex"))

    assert answer["started"] is True
    assert applied == [("A", "mb-a")]
