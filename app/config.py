"""Application settings, loaded from config/config.toml with environment overrides."""

from __future__ import annotations

import os
import tomllib
from typing import Any
from pathlib import Path

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("DC_CONFIG_DIR", ROOT / "config"))
CONFIG_FILE = CONFIG_DIR / "config.toml"

# TOML key -> environment variable that overrides it.
_ENV_OVERRIDES = {
    "spotify_client_id": "DC_SPOTIFY_CLIENT_ID",
    "spotify_client_secret": "DC_SPOTIFY_CLIENT_SECRET",
    "output_dir": "DC_OUTPUT_DIR",
    "concurrency": "DC_CONCURRENCY",
    "audio_bitrate": "DC_AUDIO_BITRATE",
    "max_attempts": "DC_MAX_ATTEMPTS",
    "rate_limit_sleep": "DC_RATE_LIMIT_SLEEP",
    "beets_enabled": "DC_BEETS_ENABLED",
    "music_dir": "DC_MUSIC_DIR",
    "navidrome_db": "DC_NAVIDROME_DB",
    "navidrome_url": "DC_NAVIDROME_URL",
    "navidrome_user": "DC_NAVIDROME_USER",
    "navidrome_password": "DC_NAVIDROME_PASSWORD",
    "staging_sweep_hour": "DC_STAGING_SWEEP_HOUR",
    "play_day_timezone": "DC_PLAY_DAY_TIMEZONE",
    "acoustid_key": "DC_ACOUSTID_KEY",
}


class Settings(BaseModel):
    spotify_client_id: str = ""
    spotify_client_secret: str = ""

    # Root of the staging area beets imports from. Albums and singles are
    # written to subdirectories so each can use a different beets import mode.
    output_dir: Path = ROOT / "untagged"

    concurrency: int = Field(default=3, ge=1, le=10)
    audio_bitrate: str = "320"
    max_attempts: int = Field(default=3, ge=1, le=10)
    # Seconds to pause between downloads, to stay under YouTube's radar.
    rate_limit_sleep: float = Field(default=2.0, ge=0)

    # Hand finished downloads to beets, which tags them against MusicBrainz
    # and files them into the library tree defined in its own config.
    beets_enabled: bool = True

    # The tagged library beets files into, and which Navidrome serves.
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

    # The local hour at which to look for anything sitting in staging that no
    # download job put there - a manual drop, or a job that finished while
    # beets was busy. Once a night, not every quarter of an hour: beets does
    # a MusicBrainz lookup per item and moves files about, and on a machine
    # serving music over a marginal wifi link that is felt as stuttering
    # playback. `beets_enabled` turns it off entirely.
    #
    # Local means `play_day_timezone`, the same setting the nightly play-count
    # snapshot uses. Midnight by default.
    staging_sweep_hour: int = Field(default=0, ge=0, le=23)

    # How long a file must sit unchanged before it is considered finished.
    # Importing a directory still being written to gets a partial album.
    staging_quiet_seconds: int = Field(default=120, ge=0)

    # Which midnight closes a listening day. Stated explicitly rather than
    # taken from the container's TZ, which is Etc/UTC and would put the
    # boundary at 8pm for a listener on the US east coast - splitting every
    # evening across two reported days. zoneinfo handles daylight saving, so
    # one day a year is 23 hours and one is 25, which is what those days
    # were.
    play_day_timezone: str = "UTC"

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
    def albums_dir(self) -> Path:
        return self.output_dir / "albums"

    @property
    def singles_dir(self) -> Path:
        return self.output_dir / "singles"

    @property
    def ledger_path(self) -> Path:
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
# leave the staging tree inconsistent.
EDITABLE = (
    "spotify_client_id", "spotify_client_secret", "concurrency",
    "audio_bitrate", "max_attempts", "rate_limit_sleep", "beets_enabled",
    "navidrome_url", "navidrome_user", "navidrome_password",
    "staging_sweep_hour", "acoustid_key",
)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(updates: dict[str, Any]) -> None:
    """Apply changes to the live settings and persist them to config.toml.

    Only a flat table of scalars is ever written, so a hand-rolled serialiser
    is enough and avoids taking a dependency for six keys.

    Anything already in the file that this app does not consider editable is
    carried across untouched. The previous version wrote only the EDITABLE
    keys, so saving from the Settings panel silently deleted any hand-set
    `staging_quiet_seconds`, `music_dir` or `output_dir` - and
    `staging_quiet_seconds` has no environment override either, so it simply
    reverted to its default on the next restart with nothing to say why.
    """
    for key, value in updates.items():
        if key in EDITABLE:
            setattr(settings, key, value)

    existing: dict[str, Any] = {}
    if CONFIG_FILE.exists():
        try:
            existing = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            # A file we cannot parse is one we should not silently discard.
            raise

    stored = {key: value for key, value in existing.items()
              if key not in EDITABLE}
    stored.update({key: getattr(settings, key) for key in EDITABLE})

    lines = [
        "# Written by Download Center. Environment variables still take",
        "# precedence over anything set here.",
        "",
        *(f"{key} = {_toml_value(value)}" for key, value in stored.items()),
        "",
    ]
    CONFIG_FILE.write_text("\n".join(lines), encoding="utf-8")


def load() -> Settings:
    raw: dict = {}
    if CONFIG_FILE.exists():
        raw = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    for key, env_var in _ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value:
            raw[key] = value

    settings = Settings(**raw)
    for directory in (CONFIG_DIR, settings.albums_dir, settings.singles_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


settings = load()
