"""Whose staging area is whose.

Two usernames can reduce to one directory name, and sharing a staging area
would file one person's downloads into the other's library. That is the
failure this module exists to prevent, so it is what these check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import workspace
from app.config import settings
from app.navidrome import Identity


@pytest.fixture(autouse=True)
def staging_root(tmp_path, monkeypatch):
    root = tmp_path / "untagged"
    root.mkdir()
    monkeypatch.setattr(settings, "output_dir", root)
    monkeypatch.setattr(workspace, "CONFIG_DIR", tmp_path / "config")
    return root


def _identity(username="alex", libraries=None, user_id="u-1"):
    return Identity(
        user_id=user_id, username=username, is_admin=False, token="t",
        subsonic_token="s", subsonic_salt="s",
        libraries=libraries if libraries is not None
        else [{"id": 1, "name": "Music", "path": "/music"}])


# --- picking a destination --------------------------------------------------

def test_a_single_library_never_asks():
    space = workspace.for_session(_identity())
    assert space.library_id == 1


def test_a_library_that_is_not_yours_is_refused():
    with pytest.raises(ValueError, match="does not belong"):
        workspace.for_session(_identity(), library_id=2)


def test_an_account_with_no_library_says_so_rather_than_guessing():
    """Reaching for music_dir here is how one person's downloads end up in
    another person's collection."""
    with pytest.raises(ValueError, match="no library"):
        workspace.for_session(_identity(libraries=[]))


def test_the_key_carries_the_library_id_not_its_name():
    """A library can be renamed, and a workspace that renames itself
    abandons its index and everything staged in it."""
    space = workspace.for_session(_identity())
    assert space.key == "alex-1"


def test_two_libraries_for_one_person_are_separate_workspaces():
    libraries = [{"id": 1, "name": "Music", "path": "/music"},
                 {"id": 2, "name": "Kelly", "path": "/kelly"}]
    one = workspace.for_session(_identity(libraries=libraries), 1)
    two = workspace.for_session(_identity(libraries=libraries), 2)

    assert one.key != two.key
    assert one.staging != two.staging
    assert one.beets_library != two.beets_library


# --- the owner marker -------------------------------------------------------

def test_prepare_writes_who_the_directory_belongs_to(staging_root):
    space = workspace.for_session(_identity())
    space.prepare()

    marker = space.staging / ".owner"
    assert marker.is_file()
    lines = marker.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "alex"
    assert lines[2] == str(Path("/music"))
    assert lines[3] == "1"


def test_prepare_refuses_a_directory_belonging_to_someone_else(staging_root):
    """Two usernames can slug to one directory name. Sharing it would file
    one person's downloads into the other's library, so this stops."""
    mine = workspace.for_session(_identity("alex"))
    mine.prepare()

    intruder = workspace.Workspace("alex.", 1, "Music", Path("/music"))
    # Force the collision the slug can produce.
    (staging_root / intruder.key).mkdir(parents=True, exist_ok=True)
    (staging_root / intruder.key / ".owner").write_text(
        "someone-else\nMusic\n/music\n1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="already belongs to"):
        intruder.prepare()


def test_prepare_is_idempotent(staging_root):
    space = workspace.for_session(_identity())
    space.prepare()
    first = (space.staging / ".owner").read_text(encoding="utf-8")
    space.prepare()
    assert (space.staging / ".owner").read_text(encoding="utf-8") == first


def test_prepare_makes_every_directory_the_pipeline_writes_into(staging_root):
    space = workspace.for_session(_identity())
    space.prepare()

    for directory in (space.albums_dir, space.singles_dir,
                      space.incomplete_dir, space.beets_dir):
        assert directory.is_dir()


def test_incomplete_sits_beside_the_finished_folders(staging_root):
    """It has to be on the same filesystem, or publishing an album is a copy
    rather than a rename and beets can see it half-written."""
    space = workspace.for_session(_identity())
    assert space.incomplete_dir.parent == space.albums_dir.parent


# --- reading workspaces back with nobody signed in --------------------------

def test_existing_reads_back_what_a_session_created(staging_root):
    """The sweep runs on a timer with nobody signed in, so the folder itself
    is the only remaining record of whose files these are."""
    workspace.for_session(_identity()).prepare()

    found = workspace.existing()
    assert len(found) == 1
    assert found[0].username == "alex"
    assert found[0].library_id == 1
    assert found[0].library_path == Path("/music")


def test_existing_ignores_a_directory_with_no_owner_marker(staging_root):
    (staging_root / "stray").mkdir()
    assert workspace.existing() == []


def test_existing_ignores_dotted_directories(staging_root):
    (staging_root / ".incomplete").mkdir()
    assert workspace.existing() == []


@pytest.mark.parametrize("username, expected", [
    ("alex", "alex"),
    ("Kelly Smith", "Kelly_Smith"),
    ("a/b", "a_b"),
    ("...", "user"),
    ("", "user"),
])
def test_a_username_becomes_something_a_filesystem_cannot_misread(
        username, expected):
    assert workspace.slug(username) == expected
