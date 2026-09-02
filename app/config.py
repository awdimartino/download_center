"""Application settings, loaded from config/config.toml with environment overrides."""

from __future__ import annotations

import os
import tomllib
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
    "audio_bitrate", "max_attempts", "rate_limit_sleep",
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
    """
    for key, value in updates.items():
        if key in EDITABLE:
            setattr(settings, key, value)

    stored = {key: getattr(settings, key) for key in EDITABLE}
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
