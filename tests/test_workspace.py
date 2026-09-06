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


# Where the default identity's library lives. A real directory, because
# for_session now refuses a library this container cannot see - which is the
# whole point of the guard, and would otherwise make every test here a test
# of that guard instead of what it means to test.
LIBRARY: Path | None = None


@pytest.fixture(autouse=True)
def staging_root(tmp_path, monkeypatch):
    global LIBRARY
    root = tmp_path / "untagged"
    root.mkdir()
    LIBRARY = tmp_path / "music"
    LIBRARY.mkdir()
    (tmp_path / "kelly").mkdir()
    monkeypatch.setattr(settings, "output_dir", root)
    monkeypatch.setattr(workspace, "CONFIG_DIR", tmp_path / "config")
    yield root
    LIBRARY = None


def _identity(username="alex", libraries=None, user_id="u-1"):
    return Identity(
        user_id=user_id, username=username, is_admin=False, token="t",
        subsonic_token="s", subsonic_salt="s",
        libraries=libraries if libraries is not None
        else [{"id": 1, "name": "Music", "path": str(LIBRARY)}])


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


def test_two_libraries_for_one_person_are_separate_workspaces(tmp_path):
    libraries = [{"id": 1, "name": "Music", "path": str(LIBRARY)},
                 {"id": 2, "name": "Kelly", "path": str(tmp_path / "kelly")}]
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
    assert lines[2] == str(LIBRARY)
    assert lines[3] == "1"


def test_prepare_refuses_a_directory_belonging_to_someone_else(staging_root):
    """Two usernames can slug to one directory name. Sharing it would file
    one person's downloads into the other's library, so this stops."""
    mine = workspace.for_session(_identity("alex"))
    mine.prepare()

    intruder = workspace.Workspace("alex.", 1, "Music", LIBRARY)
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
    assert found[0].library_path == LIBRARY


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


# --- a library this container cannot see ----------------------------------

def test_a_library_that_is_not_mounted_here_is_refused(tmp_path):
    """Navidrome reports where a library lives from its own database, and
    this is a different container with its own mounts. Add a library there
    and forget the bind mount here and the path exists for Navidrome and not
    for us - so beets creates it inside the container, files the music into
    it, reports success, and the next `docker compose pull` takes the lot.
    """
    identity = _identity(libraries=[
        {"id": 9, "name": "Test", "path": str(tmp_path / "not-mounted")}])

    with pytest.raises(ValueError, match="not mounted"):
        workspace.for_session(identity)


def test_the_refusal_names_the_path_and_says_what_to_do(tmp_path):
    missing = tmp_path / "not-mounted"
    identity = _identity(libraries=[
        {"id": 9, "name": "Test", "path": str(missing)}])

    with pytest.raises(ValueError) as caught:
        workspace.for_session(identity)

    message = str(caught.value)
    assert "Test" in message
    assert str(missing) in message
    assert "bind mount" in message


def test_a_mounted_library_is_fine(tmp_path):
    root = tmp_path / "mounted"
    root.mkdir()
    identity = _identity(libraries=[
        {"id": 9, "name": "Test", "path": str(root)}])

    assert workspace.for_session(identity).library_path == root


def test_a_file_where_a_library_should_be_is_refused(tmp_path):
    """`is_dir()`, not `exists()`. A bind mount pointing at a file is not a
    library, and treating it as one fails much later and less clearly."""
    impostor = tmp_path / "library"
    impostor.write_text("not a directory", encoding="utf-8")
    identity = _identity(libraries=[
        {"id": 9, "name": "Test", "path": str(impostor)}])

    with pytest.raises(ValueError, match="not mounted"):
        workspace.for_session(identity)


def test_read_only_callers_can_opt_out(tmp_path):
    """Deleting a job touches only the staging scratch directory, and
    forgetting a ledger row touches no files at all. Refusing those because
    the library is unmounted would strand a job with its files on disk."""
    identity = _identity(libraries=[
        {"id": 9, "name": "Test", "path": str(tmp_path / "not-mounted")}])

    space = workspace.for_session(identity, require_library=False)
    assert space.library_id == 9


def test_the_check_is_on_by_default():
    """The cost of forgetting it is music written into a container and lost;
    the cost of an unnecessary check is one keyword argument."""
    import inspect
    signature = inspect.signature(workspace.for_session)
    assert signature.parameters["require_library"].default is True


# --- naming a staged item ---------------------------------------------------
# The name comes from the browser, so this is a trust boundary: everything
# behind it moves files into a music library.

def _prepared():
    space = workspace.for_session(_identity())
    space.prepare()
    return space


def test_a_staged_single_resolves_to_its_file():
    space = _prepared()
    track = space.singles_dir / "Burial - Archangel.mp3"
    track.write_bytes(b"audio")

    assert space.staged("single", "Burial - Archangel.mp3") == track


def test_a_staged_album_resolves_to_its_directory():
    space = _prepared()
    album = space.albums_dir / "Radiohead - OK Computer"
    album.mkdir()

    assert space.staged("album", "Radiohead - OK Computer") == album.resolve()


@pytest.mark.parametrize("name", [
    "../../../etc/passwd", "..", ".owner", "sub/dir", "sub\\dir", "",
])
def test_a_name_that_leaves_staging_is_refused(name):
    """Not "does this contain ..": resolved and compared against the parent,
    so a symlink cannot walk out of a name that looked clean."""
    space = _prepared()
    with pytest.raises(ValueError):
        space.staged("single", name)


def test_a_name_that_is_not_there_is_refused():
    space = _prepared()
    with pytest.raises(ValueError, match="not in"):
        space.staged("single", "never-downloaded.mp3")


def test_an_album_asked_for_as_a_single_is_refused():
    """Kind decides both the folder and the beets mode. Letting them disagree
    would import a whole album with the singleton path template."""
    space = _prepared()
    (space.albums_dir / "A Record").mkdir()
    with pytest.raises(ValueError, match="not in"):
        space.staged("single", "A Record")


def test_an_unknown_kind_is_refused():
    space = _prepared()
    with pytest.raises(ValueError, match="unknown kind"):
        space.staged("elsewhere", "anything")
