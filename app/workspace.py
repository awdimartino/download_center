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
    def staging(self) -> Path:
        return settings.output_dir / slug(self.username)

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
        return CONFIG_DIR / "beets" / slug(self.username)

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
        marker = self.staging / ".owner"
        if not marker.exists():
            marker.write_text(f"{self.username}\n{self.library_name}\n",
                              encoding="utf-8")


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
        beets_config = CONFIG_DIR / "beets" / directory.name / "config.yaml"
        found.append(Workspace(username, 0, library_name,
                               _library_path_from(beets_config)))
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
