"""Settings persistence and session lifetime.

Both are small, and both lose something quietly: a save used to delete keys
it did not recognise, and a session was only ever forgotten if its own cookie
came back.
"""

from __future__ import annotations

import time

import pytest

from app import auth, config, navidrome


# --- config.save ------------------------------------------------------------

@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    return path


def test_saving_preserves_keys_it_does_not_manage(config_file, monkeypatch):
    """staging_quiet_seconds is not in EDITABLE and has no environment
    override, so a save used to delete it and it silently reverted to its
    default on the next restart."""
    config_file.write_text(
        'staging_quiet_seconds = 600\n'
        'music_dir = "/mnt/music"\n'
        'concurrency = 3\n', encoding="utf-8")

    monkeypatch.setattr(config.settings, "concurrency", 3)
    config.save({"concurrency": 5})

    written = config_file.read_text(encoding="utf-8")
    assert "staging_quiet_seconds = 600" in written
    assert 'music_dir = "/mnt/music"' in written
    assert "concurrency = 5" in written


def test_saving_writes_every_editable_key(config_file):
    config.save({"concurrency": 4})
    written = config_file.read_text(encoding="utf-8")
    for key in config.EDITABLE:
        assert f"{key} = " in written


def test_saving_round_trips_through_the_parser(config_file):
    config.save({"audio_bitrate": "320", "concurrency": 2})
    import tomllib
    parsed = tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert parsed["concurrency"] == 2
    assert parsed["audio_bitrate"] == "320"


def test_a_windows_path_survives_the_serialiser(config_file):
    config_file.write_text('music_dir = "C:\\\\Music"\n', encoding="utf-8")
    config.save({"concurrency": 3})
    import tomllib
    parsed = tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert parsed["music_dir"] == "C:\\Music"


def test_beets_enabled_is_editable_and_settable():
    """It was in EDITABLE and returned by GET, but absent from the update
    model - so the two disagreed about what "editable" meant."""
    from app.main import SettingsUpdate
    assert "beets_enabled" in config.EDITABLE
    assert "beets_enabled" in SettingsUpdate.model_fields


# --- sessions ---------------------------------------------------------------

def _identity(name="alex"):
    return navidrome.Identity(
        user_id=f"u-{name}", username=name, is_admin=False, token="t",
        subsonic_token="s", subsonic_salt="s", libraries=[])


@pytest.fixture(autouse=True)
def clean_sessions():
    auth._sessions.clear()
    auth._last_sweep = 0.0
    yield
    auth._sessions.clear()


def test_an_expired_session_is_swept_even_if_never_presented(monkeypatch):
    """It used to be removed only when somebody presented that exact cookie,
    so an abandoned one held a live Navidrome bearer token for a fortnight."""
    now = time.time()
    stale = auth.Session("gone", _identity("kelly"), now, now - auth.LIFETIME_SECONDS - 1)
    live = auth.Session("here", _identity("alex"), now, now)
    auth._sessions[stale.id] = stale
    auth._sessions[live.id] = live
    auth._last_sweep = 0.0

    assert auth.get("here") is live
    assert "gone" not in auth._sessions


def test_sweeping_is_rate_limited(monkeypatch):
    """Otherwise every request walks the dict."""
    now = time.time()
    live = auth.Session("here", _identity(), now, now)
    auth._sessions["here"] = live
    auth.get("here")
    first = auth._last_sweep

    auth.get("here")
    assert auth._last_sweep == first, "should not sweep twice in a row"


def test_presenting_an_expired_cookie_still_refuses(monkeypatch):
    now = time.time()
    stale = auth.Session("old", _identity(), now,
                         now - auth.LIFETIME_SECONDS - 1)
    auth._sessions["old"] = stale
    assert auth.get("old") is None


def test_a_live_session_has_its_last_seen_bumped():
    now = time.time()
    session = auth.Session("here", _identity(), now, now - 100)
    auth._sessions["here"] = session
    auth.get("here")
    assert session.last_seen > now - 100


def test_no_cookie_is_not_a_session():
    assert auth.get(None) is None
    assert auth.get("") is None
