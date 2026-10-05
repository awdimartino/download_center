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


# --- privileges are re-read (CODE_REVIEW M33) -------------------------------
# Admin status and libraries were fixed at sign-in, and the lifetime slid for
# as long as the session was used: a demoted admin stayed admin and a revoked
# library stayed editable.

def _due(session):
    session.libraries_checked_at = 0.0
    auth._sessions[session.id] = session
    return session


def test_a_demotion_and_a_revoked_library_reach_the_session(monkeypatch):
    now = time.time()
    me = _identity()
    me.is_admin = True
    me.libraries = [{"id": 1, "name": "Music", "path": "/music"}]
    _due(auth.Session("here", me, now, now))
    monkeypatch.setattr(navidrome, "account", lambda identity: (False, []))

    session = auth.get("here")

    assert session.identity.is_admin is False
    assert session.identity.libraries == []


def test_an_account_deleted_in_navidrome_signs_out(monkeypatch):
    now = time.time()
    _due(auth.Session("here", _identity(), now, now))
    monkeypatch.setattr(navidrome, "account", lambda identity: None)

    assert auth.get("here") is None
    assert "here" not in auth._sessions


def test_an_unreadable_database_changes_nothing(monkeypatch):
    now = time.time()
    me = _identity()
    me.is_admin = True
    me.libraries = [{"id": 1, "name": "Music", "path": "/music"}]
    _due(auth.Session("here", me, now, now))

    def unreadable(identity):
        raise navidrome.Unavailable("locked")

    monkeypatch.setattr(navidrome, "account", unreadable)
    session = auth.get("here")

    assert session.identity.is_admin is True
    assert session.identity.libraries


def test_a_session_ends_a_month_after_sign_in_however_busy():
    now = time.time()
    auth._sessions["old"] = auth.Session(
        "old", _identity(), now - auth.MAX_AGE_SECONDS - 1, now)
    assert auth.get("old") is None


def test_account_reads_the_admin_flag_and_libraries(navidrome_db, monkeypatch):
    import sqlite3

    from app.config import settings

    monkeypatch.setattr(settings, "navidrome_db", navidrome_db)
    connection = sqlite3.connect(navidrome_db)
    with connection:
        connection.execute('ALTER TABLE "user" ADD COLUMN is_admin INTEGER DEFAULT 0')
        connection.execute('UPDATE "user" SET is_admin = 1 WHERE id = \'u-kelly\'')
    connection.close()

    assert navidrome.account(_identity("kelly"))[0] is True
    assert navidrome.account(_identity("alex")) == (
        False, [{"id": 1, "name": "Music", "path": str(navidrome_db.parent / "music")}])
    assert navidrome.account(_identity("nobody")) is None


# --- the collector's status is an administrator's (L25) ------------------------

def test_the_playcount_status_needs_an_administrator():
    """Its totals sum every account's imported plays."""
    from app import main

    route = next(r for r in main.app.routes
                 if getattr(r, "path", None) == "/api/playcounts"
                 and "GET" in r.methods)
    dependencies = {d.call for d in route.dependant.dependencies}
    assert main.admin_session in dependencies


# --- the generated API docs need a session (L32) -------------------------------

def _through_the_gate(path, cookie=None):
    import asyncio

    from starlette.requests import Request

    from app import main

    headers = [(b"cookie", f"dc_session={cookie}".encode())] if cookie else []
    request = Request({"type": "http", "method": "GET", "path": path,
                       "headers": headers, "query_string": b""})

    async def call_next(request):
        return "served"

    return asyncio.run(main.require_session(request, call_next))


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_the_api_docs_are_not_open_to_anyone(path):
    response = _through_the_gate(path)
    assert getattr(response, "status_code", None) == 401


def test_the_page_itself_is_still_open():
    assert _through_the_gate("/") == "served"


def test_a_signed_in_person_can_read_the_docs(monkeypatch):
    from app import auth

    session = _session(days_since_sign_in=0, days_since_cookie=0)
    monkeypatch.setattr(auth, "get", lambda sid: session if sid == "s1" else None)
    assert _through_the_gate("/docs", cookie="s1") == "served"


# --- the cookie keeps up with the session (L33) ---------------------------------

def _request_with(session, monkeypatch):
    import asyncio

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from app import auth, main

    monkeypatch.setattr(auth, "get", lambda sid: session if sid == session.id else None)
    request = Request({"type": "http", "method": "GET", "path": "/api/library",
                       "headers": [(b"cookie", f"dc_session={session.id}".encode())],
                       "query_string": b"", "scheme": "http"})

    async def call_next(request):
        return JSONResponse({"ok": True})

    return asyncio.run(main.require_session(request, call_next))


def _session(days_since_sign_in, days_since_cookie):
    import time

    from app import auth, navidrome

    now = time.time()
    identity = navidrome.Identity(user_id="u", username="alex", is_admin=False,
                                  token="t", subsonic_token="st",
                                  subsonic_salt="ss", libraries=[])
    return auth.Session("s1", identity, now - days_since_sign_in * 86400, now,
                        now, cookie_sent_at=now - days_since_cookie * 86400)


def test_an_active_session_has_its_cookie_renewed(monkeypatch):
    """Sent once at sign-in with a 14-day max-age, the cookie was dropped on
    day 14 while the server's session, sliding with use, was still good."""
    from app import auth

    session = _session(days_since_sign_in=10, days_since_cookie=10)
    response = _request_with(session, monkeypatch)

    cookie = response.headers.get("set-cookie", "")
    assert "dc_session=s1" in cookie
    assert f"Max-Age={auth.LIFETIME_SECONDS}" in cookie
    assert not auth.cookie_due(session)


def test_a_renewed_cookie_never_outlives_the_hard_cap(monkeypatch):
    session = _session(days_since_sign_in=29.5, days_since_cookie=2)
    cookie = _request_with(session, monkeypatch).headers.get("set-cookie", "")
    age = int(cookie.split("Max-Age=")[1].split(";")[0])
    assert 43000 < age <= 43200


def test_a_cookie_sent_today_is_not_sent_again(monkeypatch):
    session = _session(days_since_sign_in=3, days_since_cookie=0.1)
    assert "set-cookie" not in _request_with(session, monkeypatch).headers


# --- clearing a setting, and turning beets off (L35) ----------------------------

@pytest.mark.asyncio
async def test_an_emptied_field_clears_the_setting(config_file, monkeypatch):
    from types import SimpleNamespace

    from app import main

    monkeypatch.setattr(config.settings, "spotify_client_id", "abc123")
    monkeypatch.setattr(config.settings, "beets_enabled", True)
    monkeypatch.setattr(config, "FROM_ENV", set())
    admin = SimpleNamespace(identity=SimpleNamespace(is_admin=True))

    await main.put_settings(
        main.SettingsUpdate(spotify_client_id="", beets_enabled=False), admin)

    assert config.settings.spotify_client_id == ""
    assert config.settings.beets_enabled is False
    written = config_file.read_text(encoding="utf-8")
    assert 'spotify_client_id = ""' in written
    assert "beets_enabled = false" in written


def test_the_form_can_clear_text_and_untick_beets():
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "app" / "static"
    js = (static / "js" / "settings.js").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")
    assert '<input name="beets_enabled" type="checkbox">' in html
    assert 'const CLEARABLE = ["spotify_client_id", "navidrome_url", "navidrome_user"]' in js
    assert "payload[box.name] = box.checked" in js


# --- a change has to come from this app's own page (L38) ------------------------

def _post(path, headers):
    import asyncio

    from starlette.requests import Request

    from app import main

    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    request = Request({"type": "http", "method": "POST", "path": path,
                       "headers": raw, "query_string": b""})

    async def call_next(request):
        return "served"

    return asyncio.run(main.require_session(request, call_next))


@pytest.mark.parametrize("headers", [
    {"sec-fetch-site": "same-site", "host": "pi:8000", "origin": "http://pi:4533"},
    {"host": "pi:8000", "origin": "http://pi:4533"},
    {"sec-fetch-site": "cross-site", "host": "pi:8000"},
])
def test_a_post_from_another_page_is_refused(headers):
    """Navidrome on :4533 is the same *site* as this on :8000, so the Lax
    cookie went along with its POSTs."""
    response = _post("/api/library/rescan", headers)
    assert getattr(response, "status_code", None) == 403


@pytest.mark.parametrize("headers", [
    {"sec-fetch-site": "same-origin", "host": "pi:8000", "origin": "http://pi:8000"},
    {"host": "pi:8000", "origin": "http://pi:8000"},
    {"host": "localhost:8000", "x-forwarded-host": "music.example.com",
     "origin": "https://music.example.com"},
    {"host": "pi:8000"},
])
def test_a_post_from_this_page_or_no_browser_goes_through(headers):
    # Signed out, so it reaches the session check rather than the handler.
    response = _post("/api/library/rescan", headers)
    assert getattr(response, "status_code", None) == 401


def test_a_socket_opened_by_another_page_is_closed(monkeypatch):
    import asyncio

    from app import auth, main

    monkeypatch.setattr(auth, "get", lambda sid: object())
    events = []

    class Socket:
        cookies = {"dc_session": "s1"}
        headers = {"host": "pi:8000", "origin": "http://pi:4533"}

        async def accept(self):
            events.append("accept")

        async def close(self, code):
            events.append(code)

    asyncio.run(main.websocket(Socket()))
    assert events == ["accept", 4403]


# --- what sign-in tells a stranger, and how often (L39) -------------------------

def _sign_in(monkeypatch, outcome, address="10.0.0.9"):
    import asyncio

    from starlette.requests import Request
    from starlette.responses import Response

    from app import auth, main

    def attempt(username, password):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(auth, "sign_in", attempt)
    request = Request({"type": "http", "method": "POST", "path": "/api/auth/login",
                       "headers": [], "query_string": b"", "client": (address, 5000),
                       "scheme": "http"})
    return asyncio.run(main.sign_in(request, main.LoginRequest(
        username="alex", password="pw"), Response()))


@pytest.fixture
def no_failures(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "_sign_in_failures", {}, raising=False)


def test_an_unreachable_navidrome_is_not_described_to_a_stranger(monkeypatch,
                                                                 no_failures):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as refused:
        _sign_in(monkeypatch, OSError("connect to 172.18.0.4:4533 refused"))

    assert refused.value.status_code == 502
    assert "172.18" not in refused.value.detail


def test_repeated_failed_sign_ins_are_slowed_down(monkeypatch, no_failures):
    from fastapi import HTTPException

    from app import main, navidrome

    for _ in range(main.SIGN_IN_FAILURES):
        with pytest.raises(HTTPException) as refused:
            _sign_in(monkeypatch, navidrome.LoginFailed("Incorrect username or password."))
        assert refused.value.status_code == 401

    with pytest.raises(HTTPException) as refused:
        _sign_in(monkeypatch, navidrome.LoginFailed("Incorrect username or password."))
    assert refused.value.status_code == 429

    # Somebody else, from another address, is not held up.
    with pytest.raises(HTTPException) as other:
        _sign_in(monkeypatch, navidrome.LoginFailed("x"), address="10.0.0.10")
    assert other.value.status_code == 401


def test_owner_checked_cover_art_is_not_cached_for_everyone():
    """A shared cache would serve one person's art to another (L40)."""
    from app import main

    assert main.ART_CACHE.startswith("private,")


# --- the download settings are checked (L44) -----------------------------------

@pytest.mark.parametrize("value, kept", [("320", "320"), (192, "192"),
                                         ("128k", "128"), ("0", "0")])
def test_a_real_bitrate_is_accepted(value, kept):
    assert config.Settings(audio_bitrate=value).audio_bitrate == kept


@pytest.mark.parametrize("value", ["loud", "1000", "16", "", "-5"])
def test_a_bitrate_ffmpeg_cannot_use_is_refused(value):
    with pytest.raises(ValueError):
        config.Settings(audio_bitrate=value)


def test_the_pause_between_downloads_is_bounded():
    with pytest.raises(ValueError):
        config.Settings(rate_limit_sleep=2000)


@pytest.mark.asyncio
async def test_a_saved_setting_is_stored_as_the_model_reads_it(
        config_file, monkeypatch):
    """"320k" validates because the model drops the k. Storing the raw text
    left the live setting as "320k", which failed every download until a
    restart read the file back through the model."""
    from types import SimpleNamespace

    from app import main

    monkeypatch.setattr(config.settings, "audio_bitrate", "192")
    monkeypatch.setattr(config, "FROM_ENV", set())
    admin = SimpleNamespace(identity=SimpleNamespace(is_admin=True))

    await main.put_settings(main.SettingsUpdate(audio_bitrate="320k"), admin)

    assert config.settings.audio_bitrate == "320"
    assert 'audio_bitrate = "320"' in config_file.read_text(encoding="utf-8")
