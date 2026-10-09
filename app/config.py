"""Application settings, loaded from config/config.toml with environment overrides."""

from __future__ import annotations

import datetime
import logging
import os
import re
import tomllib
from typing import Any
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("navidrome_companion")

# The environment variables' prefix. It was DC_, for Download Center, until
# 2026-10-09; the old names are still read, with a warning, so an existing
# .env or compose file keeps working while it is updated.
PREFIX = "NC_"
OLD_PREFIX = "DC_"
_warned_old: set[str] = set()


def environ(name: str, default: str | None = None) -> str | None:
    """An NC_ variable by its name without the prefix, or its DC_ spelling.

    The new name wins when both are set. The old one is reported once per
    process, by name, so the log says exactly what to rename. Empty counts
    as unset: compose passes `NC_X: ${NC_X:-}` through as an empty string
    when nothing sets it, which would otherwise hide a DC_X beside it.
    """
    value = os.environ.get(PREFIX + name)
    if value:
        return value
    value = os.environ.get(OLD_PREFIX + name)
    if value:
        if name not in _warned_old:
            _warned_old.add(name)
            log.warning("%s%s is deprecated; rename it %s%s", OLD_PREFIX, name,
                        PREFIX, name)
        return value
    return default


ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(environ("CONFIG_DIR") or ROOT / "config")
CONFIG_FILE = CONFIG_DIR / "config.toml"

# TOML key -> environment variable that overrides it: NC_ and the key in
# capitals, for every key that can be overridden.
_ENV_OVERRIDES = {key: PREFIX + key.upper() for key in (
    "spotify_client_id", "spotify_client_secret", "output_dir", "concurrency",
    "audio_bitrate", "max_attempts", "rate_limit_sleep", "beets_enabled",
    "inbox_quiet_seconds", "music_dir", "navidrome_db", "navidrome_url",
    "navidrome_user", "navidrome_password", "play_day_timezone",
    "acoustid_key", "lastfm_api_key", "lastfm_secret",
)}


class Settings(BaseModel):
    spotify_client_id: str = ""
    spotify_client_secret: str = ""

    # Where each person's workspace lives: their inbox, the scratch space a
    # download is built in, and the marker saying whose it is.
    output_dir: Path = ROOT / "workspace"

    concurrency: int = Field(default=3, ge=1, le=10)
    # Handed to ffmpeg through yt-dlp: a constant bitrate in kbps, or a VBR
    # level from 0 (best) to 9. Anything else used to be accepted here and
    # fail every download at the encoding step.
    audio_bitrate: str = "320"
    max_attempts: int = Field(default=3, ge=1, le=10)
    # Seconds to pause between downloads, to stay under YouTube's radar.
    # Bounded: a typo of 2000 held a download slot for half an hour a track.
    rate_limit_sleep: float = Field(default=2.0, ge=0, le=300)

    @field_validator("audio_bitrate", mode="before")
    @classmethod
    def _bitrate(cls, value: Any) -> str:
        text = str(value).strip().lower().removesuffix("k")
        if text.isdigit() and (int(text) <= 9 or 32 <= int(text) <= 320):
            return str(int(text))
        raise ValueError("audio_bitrate must be a bitrate from 32 to 320 "
                         "(kbps), or a VBR level from 0 to 9")

    # Whether Library offers to match an album against MusicBrainz.
    # Beets no longer files anything: a download goes into the library on its
    # own, and this only decides whether the "Find matches" button does
    # something when you press it.
    beets_enabled: bool = True

    # The library the filer files into, and which Navidrome serves.
    music_dir: Path = Path("/music")

    # Navidrome's database, mounted read-only. Health reporting reads it;
    # nothing here ever writes to it, because Navidrome owns that file and
    # caches from it. State changes go through Navidrome's HTTP API.
    navidrome_db: Path = Path("/navidrome/navidrome.db")

    # Navidrome's API, used to ask for a scan once beets has filed something.
    # Optional: without it new music simply waits for Navidrome's own
    # schedule instead of appearing straight away.
    navidrome_url: str = ""
    navidrome_user: str = ""
    navidrome_password: str = ""

    # How long a file must sit unchanged before the inbox files it.
    # Nothing here can know whether something is mid-copy - a file arriving
    # over a network share is written by a machine this one cannot ask - so
    # it waits for stillness instead. A download does not wait: the worker
    # delivers it and files it in the same breath, because it knows.
    inbox_quiet_seconds: int = Field(default=120, ge=0)

    # Which midnight closes a listening day. Stated explicitly rather than
    # taken from the container's TZ, which is Etc/UTC and would put the
    # boundary at 8pm for a listener on the US east coast - splitting every
    # evening across two reported days. zoneinfo handles daylight saving, so
    # one day a year is 23 hours and one is 25, which is what those days
    # were.
    play_day_timezone: str = "UTC"

    # The same pair Navidrome uses (ND_LASTFM_APIKEY / ND_LASTFM_SECRET),
    # needed only by the scrobble tooling in app/lastfm.py. The session key
    # itself is Navidrome's and is read from its database, so nothing here
    # stores a credential that belongs to a person.
    lastfm_api_key: str = ""
    lastfm_secret: str = ""

    # An AcoustID application key, for `tools/fingerprint.py` - the backfill
    # that identifies already-filed files by what they sound like, for the
    # ones whose tags are too poor to match on.
    #
    # Not needed for ordinary imports. The beets chroma plugin carries its
    # own client key and identifies new music without this being set; only
    # the bulk backfill, which is a separate application making its own
    # requests, needs one of your own. Free, from
    # https://acoustid.org/new-application - and left blank, the backfill
    # simply says so rather than running against somebody else's key.
    acoustid_key: str = ""

    @property
    def state_db(self) -> Path:
        return CONFIG_DIR / "state.db"

    @property
    def cookies_file(self) -> Path | None:
        path = CONFIG_DIR / "cookies.txt"
        return path if path.exists() else None

    @property
    def spotify_configured(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)


# Settings a user may change from the browser. Everything else needs a file
# edit, either because it is a secret or because changing it mid-flight would
# leave the workspace or the library inconsistent.
EDITABLE = (
    "spotify_client_id", "spotify_client_secret", "concurrency",
    "audio_bitrate", "max_attempts", "rate_limit_sleep", "beets_enabled",
    "navidrome_url", "navidrome_user", "navidrome_password",
    "acoustid_key",
)


# Keys whose value came from an environment variable at start-up. They are
# never written to config.toml - saving the panel used to copy a password
# set as NC_NAVIDROME_PASSWORD into the file in plain text - and they cannot
# be changed from the panel, since the environment wins again on restart and
# the edit would silently revert.
FROM_ENV: set[str] = set()


def env_var(key: str) -> str:
    """The environment variable that sets `key`."""
    return _ENV_OVERRIDES.get(key, "")


# What a TOML basic string must escape. Only backslash and quote used to be,
# so a newline pasted into a field wrote a file tomllib refuses - and config
# is loaded at import, so the container then failed to start, again and again.
_TOML_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t",
                 "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _toml_string(text: str) -> str:
    out = []
    for char in text:
        if char in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _toml_string(key)


def _toml_value(value: Any) -> str:
    """A value as TOML. Tables, arrays and dates too: a hand-written table in
    config.toml used to come back from the next save as the string of a
    Python dict, and whatever read it then got text instead of a table."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        inner = ", ".join(f"{_toml_key(str(k))} = {_toml_value(v)}"
                          for k, v in value.items())
        return "{ " + inner + " }" if inner else "{}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    return _toml_string(str(value))


def save(updates: dict[str, Any]) -> None:
    """Apply changes to the live settings and persist them to config.toml.

    Only a flat table of scalars is ever written, so a hand-rolled serialiser
    is enough and avoids taking a dependency for six keys.

    Anything already in the file that this app does not consider editable is
    carried across untouched. The previous version wrote only the EDITABLE
    keys, so saving from the Settings panel silently deleted any hand-set
    `inbox_quiet_seconds`, `music_dir` or `output_dir`, any of which would
    then silently revert to its default on the next restart with nothing to
    say why.
    """
    locked = sorted(key for key in updates if key in FROM_ENV)
    if locked:
        raise ValueError(
            f"{locked[0]} is set by {env_var(locked[0])} in the container's "
            "environment; change it there.")
    # Applied to the live settings only once the file has been written. They
    # used to be applied first: a file that would not parse, or a /config
    # that could not be written, answered with an error while the change was
    # already in force - until the next restart quietly undid it.
    changes = {key: value for key, value in updates.items() if key in EDITABLE}

    existing: dict[str, Any] = {}
    if CONFIG_FILE.exists():
        try:
            existing = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            # A file we cannot parse is one we should not silently discard.
            raise

    # A key set by the environment keeps whatever the file already said,
    # if anything; its live value is never copied in.
    stored = {key: value for key, value in existing.items()
              if key not in EDITABLE or key in FROM_ENV}
    stored.update({key: changes.get(key, getattr(settings, key))
                   for key in EDITABLE if key not in FROM_ENV})

    lines = [
        "# Written by Navidrome Companion. Environment variables still take",
        "# precedence over anything set here.",
        "",
        *(f"{_toml_key(key)} = {_toml_value(value)}"
          for key, value in stored.items()),
        "",
    ]
    text = "\n".join(lines)
    # Checked before it can replace a working file, then swapped in whole:
    # a write cut short (a full disk, a restart mid-save) used to leave a
    # truncated config.toml, and the container would not start on it.
    tomllib.loads(text)
    partial = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, CONFIG_FILE)
    for key, value in changes.items():
        setattr(settings, key, value)


def load() -> Settings:
    raw: dict = {}
    if CONFIG_FILE.exists():
        raw = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    FROM_ENV.clear()
    for key, name in _ENV_OVERRIDES.items():
        value = environ(name.removeprefix(PREFIX))
        if value:
            raw[key] = value
            FROM_ENV.add(key)

    settings = Settings(**raw)
    for directory in (CONFIG_DIR, settings.output_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


settings = load()
