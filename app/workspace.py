"""Where each person's downloads are staged, tagged and filed.

One shared staging area cannot work once more than one person uses this. The
sweep imports whatever it finds without a job to explain it - dropped in by
hand, or left behind by a job that finished while beets was busy - so the
folder itself has to say who the files belong to. There is nowhere else for
that fact to live by the time the sweep runs.

Beets needs splitting the same way, and not for tidiness: it stores item
paths relative to its `directory`, so one library database genuinely cannot
describe two roots. Each person gets their own configuration and their own
database, generated from one template with their library path filled in.

Nothing here is configured per user. The destination comes from Navidrome's
own record of which library that account can see, so a new person needs no
setup at all - they sign in, and where their music goes is already known.
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG_DIR, settings

log = logging.getLogger("download_center.workspace")

# Usernames come from Navidrome and end up as directory names, so they are
# reduced to something a filesystem cannot misread. Two accounts differing
# only in punctuation would collide, which is why the mapping is recorded in
# the workspace itself rather than assumed to be reversible.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(username: str) -> str:
    cleaned = _UNSAFE.sub("_", (username or "").strip()).strip("._-")
    return cleaned or "user"


@dataclass(frozen=True)
class Workspace:
    """One person's staging area, beets installation and destination."""

    username: str
    library_id: int
    library_name: str
    library_path: Path

    @property
    def key(self) -> str:
        """The directory name for this workspace.

        A workspace is a person *and* a library, because the beets config it
        owns hard-codes one destination - so the name carries the library id.
        The id and not the name: a library can be renamed, and a workspace
        that renames itself abandons its index and everything staged in it.

        A directory already claiming this exact pair keeps its name, which is
        what stops an installation from before libraries were part of the key
        being orphaned the moment a second library appears.
        """
        canonical = f"{slug(self.username)}-{self.library_id}"
        plain = slug(self.username)
        if plain != canonical and _claimed_by(
                settings.output_dir / plain, self.username, self.library_path):
            return plain
        return canonical

    @property
    def staging(self) -> Path:
        return settings.output_dir / self.key

    @property
    def albums_dir(self) -> Path:
        return self.staging / "albums"

    @property
    def singles_dir(self) -> Path:
        return self.staging / "singles"

    @property
    def incomplete_dir(self) -> Path:
        # Beside the finished folders, on the same filesystem, so publishing
        # an album is a rename rather than a copy.
        return self.staging / ".incomplete"

    @property
    def beets_dir(self) -> Path:
        return CONFIG_DIR / "beets" / self.key

    @property
    def beets_config(self) -> Path:
        return self.beets_dir / "config.yaml"

    @property
    def beets_library(self) -> Path:
        return self.beets_dir / "library.db"

    def prepare(self) -> None:
        for directory in (self.albums_dir, self.singles_dir,
                          self.incomplete_dir, self.beets_dir):
            directory.mkdir(parents=True, exist_ok=True)
        # A note of who this belongs to, for anyone reading the disk later.
        # Enough for the sweep - which runs with nobody signed in - to
        # rebuild this workspace exactly, including where the music goes.
        marker = self.staging / ".owner"
        owner = _owner_of(marker)
        if owner and owner[0] != self.username:
            # Two usernames can reduce to one directory name. Sharing a
            # staging area would file one person's downloads into the other's
            # library, so this stops rather than guessing.
            raise ValueError(
                f"{self.staging} already belongs to {owner[0]!r}, so "
                f"{self.username!r} cannot use it.")
        wanted = (f"{self.username}\n{self.library_name}\n"
                  f"{self.library_path}\n{self.library_id}\n")
        current = marker.read_text(encoding="utf-8") if marker.exists() else None
        if current != wanted:
            marker.write_text(wanted, encoding="utf-8")


def for_session(identity, library_id: int | None = None) -> Workspace:
    """The workspace of whoever is signed in.

    Someone with a single library never chooses; someone with several picks
    per job, and anything they did not ask for is refused rather than guessed.
    """
    libraries = identity.libraries
    if not libraries:
        raise ValueError(
            f"{identity.username} has no library in Navidrome, so there is "
            "nowhere to put a download.")

    if library_id is None:
        library = libraries[0]
    else:
        library = next((lib for lib in libraries
                        if str(lib["id"]) == str(library_id)), None)
        if library is None:
            raise ValueError("That library does not belong to this account.")

    return Workspace(identity.username, library["id"], library["name"],
                     Path(library["path"]))


def existing() -> list[Workspace]:
    """Workspaces already on disk, for work that runs with nobody signed in.

    The staging sweep has no session - it runs on a timer - so it reads back
    what previous sessions created. A person who has never signed in has no
    staging directory and so nothing that could need importing.
    """
    root = settings.output_dir
    if not root.is_dir():
        return []

    found = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        marker = directory / ".owner"
        if not marker.exists():
            continue
        lines = marker.read_text(encoding="utf-8").splitlines()
        username = lines[0].strip() if lines else directory.name
        library_name = lines[1].strip() if len(lines) > 1 else ""
        # The marker records the destination; a marker written by an older
        # version may not, so the beets config is the fallback - it is the
        # other place the answer was written down.
        recorded = lines[2].strip() if len(lines) > 2 else ""
        library_id = (int(lines[3]) if len(lines) > 3
                      and lines[3].strip().isdigit() else 0)
        beets_config = CONFIG_DIR / "beets" / directory.name / "config.yaml"
        found.append(Workspace(
            username, library_id, library_name,
            Path(recorded) if recorded else _library_path_from(beets_config)))
    return found


def adopt_legacy(space: Workspace) -> bool:
    """Move a single-user beets installation into its owner's workspace.

    Before there were accounts, one beets configuration and one library
    database sat directly in the config directory. Leaving them there would
    strand an index of the whole library while the first person to sign in
    started an empty one and re-imported everything.

    Moved rather than copied, and only when the destination is empty, so this
    can run on every start and do nothing the second time. The database is
    portable as-is: beets stores item paths relative to `directory`, and the
    directory has not changed.
    """
    legacy = CONFIG_DIR / "beets"
    legacy_config, legacy_db = legacy / "config.yaml", legacy / "library.db"
    if not legacy_db.exists() and not legacy_config.exists():
        return False
    if space.beets_config.exists() or space.beets_library.exists():
        return False

    space.prepare()
    moved = []
    for source, target in ((legacy_config, space.beets_config),
                           (legacy_db, space.beets_library),
                           (legacy / "import.log", space.beets_dir / "import.log")):
        if source.exists():
            source.rename(target)
            moved.append(target.name)

    # The configuration names its database and log by absolute path, so
    # moving the files without rewriting it leaves beets building a fresh,
    # empty index at the old location while the real one sits unread beside
    # it - and nothing that depends on knowing what was just filed works.
    if space.beets_config.exists():
        text = space.beets_config.read_text(encoding="utf-8")
        for old, new in ((legacy / "library.db", space.beets_library),
                         (legacy / "import.log", space.beets_dir / "import.log")):
            text = text.replace(str(old), str(new))
            text = text.replace(old.as_posix(), new.as_posix())
        # `directory` too. An adopted config naming a destination other than
        # this workspace's would have beets file music somewhere the stamper
        # then fails to find, because it resolves relative paths against the
        # workspace's own root.
        text = re.sub(r"(?m)^directory:.*$",
                      f"directory: {space.library_path}", text)
        space.beets_config.write_text(text, encoding="utf-8")

    log.info("adopted the previous beets installation for %s: %s",
             space.username, ", ".join(moved))
    return True


def adopt_legacy_staging(space: Workspace) -> bool:
    """Move a single-user staging area into its owner's workspace.

    Same reasoning: files sitting in the old shared albums/ and singles/
    folders belong to whoever was using this before accounts existed, and
    the sweep now only looks inside a person's own staging directory.
    """
    moved = 0
    for name in ("albums", "singles"):
        old = settings.output_dir / name
        new = getattr(space, f"{name}_dir")
        if not old.is_dir() or old == new:
            continue
        new.mkdir(parents=True, exist_ok=True)
        for entry in list(old.iterdir()):
            target = new / entry.name
            if target.exists():
                continue
            entry.rename(target)
            moved += 1
        with contextlib.suppress(OSError):
            old.rmdir()
    if moved:
        log.info("moved %d staged item(s) into %s's workspace",
                 moved, space.username)
    return bool(moved)


def _library_path_from(beets_config: Path) -> Path:
    """Read a workspace's destination back out of its beets config.

    The sweep cannot ask Navidrome which library this person has - nobody is
    signed in - so the answer is taken from the configuration that was
    written when they were.
    """
    try:
        import yaml
        raw = yaml.safe_load(beets_config.read_text(encoding="utf-8")) or {}
        directory = raw.get("directory")
        if directory:
            return Path(directory)
    except Exception as exc:
        log.warning("could not read %s: %s", beets_config, exc)
    return settings.music_dir


def _owner_of(marker: Path) -> tuple[str, Path] | None:
    """Who a staging directory says it belongs to, and where its music goes."""
    if not marker.is_file():
        return None
    lines = marker.read_text(encoding="utf-8").splitlines()
    if not lines:
        return None
    recorded = lines[2].strip() if len(lines) > 2 else ""
    return lines[0].strip(), Path(recorded) if recorded else Path()


def _claimed_by(staging: Path, username: str, library_path: Path) -> bool:
    owner = _owner_of(staging / ".owner")
    return owner is not None and owner[0] == username and owner[1] == library_path
