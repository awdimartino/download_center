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
    """inbox_quiet_seconds is not in EDITABLE and has no environment
    override, so a save used to delete it and it silently reverted to its
    default on the next restart."""
    config_file.write_text(
        'inbox_quiet_seconds = 600\n'
        'music_dir = "/mnt/music"\n'
        'concurrency = 3\n', encoding="utf-8")

    monkeypatch.setattr(config.settings, "concurrency", 3)
    config.save({"concurrency": 5})

    written = config_file.read_text(encoding="utf-8")
    assert "inbox_quiet_seconds = 600" in written
    assert 'music_dir = "/mnt/music"' in written
    assert "concurrency = 5" in written


def test_saving_writes_every_editable_key(config_file, monkeypatch):
    monkeypatch.setattr(config, "FROM_ENV", set())
    config.save({"concurrency": 4})
    written = config_file.read_text(encoding="utf-8")
    for key in config.EDITABLE:
        assert f"{key} = " in written


# --- keys set by the environment (CODE_REVIEW M25) --------------------------
# Saving the panel wrote every editable key's live value, so a password set
# as DC_NAVIDROME_PASSWORD landed in config.toml in plain text.

def test_a_secret_from_the_environment_is_never_written(config_file, monkeypatch):
    monkeypatch.setattr(config, "FROM_ENV", {"navidrome_password"})
    monkeypatch.setattr(config.settings, "navidrome_password", "from-env")
    config.save({"concurrency": 4})
    written = config_file.read_text(encoding="utf-8")
    assert "from-env" not in written
    assert "navidrome_password" not in written


def test_a_file_value_for_an_environment_key_is_kept(config_file, monkeypatch):
    config_file.write_text('navidrome_password = "from-file"\n', encoding="utf-8")
    monkeypatch.setattr(config, "FROM_ENV", {"navidrome_password"})
    monkeypatch.setattr(config.settings, "navidrome_password", "from-env")
    config.save({"concurrency": 4})
    assert 'navidrome_password = "from-file"' in config_file.read_text(encoding="utf-8")


def test_a_key_set_by_the_environment_cannot_be_changed_here(config_file,
                                                              monkeypatch):
    monkeypatch.setattr(config, "FROM_ENV", {"navidrome_url"})
    with pytest.raises(ValueError, match="DC_NAVIDROME_URL"):
        config.save({"navidrome_url": "http://elsewhere"})


def test_load_remembers_which_keys_came_from_the_environment(tmp_path,
                                                             monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "none.toml")
    monkeypatch.setenv("DC_NAVIDROME_USER", "svc")
    monkeypatch.setattr(config, "FROM_ENV", set())
    loaded = config.load()
    assert loaded.navidrome_user == "svc"
    assert "navidrome_user" in config.FROM_ENV


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


def test_the_acoustid_key_is_editable_and_settable():
    from app.main import SettingsUpdate
    assert "acoustid_key" in config.EDITABLE
    assert "acoustid_key" in SettingsUpdate.model_fields


def test_the_acoustid_key_is_never_sent_to_a_browser():
    """It identifies an application to a service that rate-limits and bans by
    key. Nothing in the page needs to read it back, so it goes out masked with
    the passwords rather than in the clear with the Spotify client id."""
    from app.main import SECRETS
    assert "acoustid_key" in SECRETS


def test_every_secret_is_an_editable_setting():
    """A key in SECRETS but not in EDITABLE is masked on a form that cannot
    save it, which reads as the field being broken."""
    for key in __import__("app.main", fromlist=["SECRETS"]).SECRETS:
        assert key in config.EDITABLE, key


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


# --- a config that always loads (CODE_REVIEW M26) ---------------------------
# Only backslash and quote were escaped, so a newline wrote invalid TOML; the
# write was not atomic; and config loads at import, so either was a crash
# loop.

@pytest.mark.parametrize("value", ["line one\nline two", "tab\there",
                                   "bell\x07", 'quote " and \ slash'])
def test_any_text_round_trips(config_file, monkeypatch, value):
    import tomllib

    monkeypatch.setattr(config, "FROM_ENV", set())
    config.save({"navidrome_user": value})

    parsed = tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert parsed["navidrome_user"] == value


def test_a_save_leaves_no_partial_file(config_file, monkeypatch):
    monkeypatch.setattr(config, "FROM_ENV", set())
    config.save({"concurrency": 2})
    assert [p.name for p in config_file.parent.iterdir()] == ["config.toml"]
