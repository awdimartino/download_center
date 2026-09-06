"""Health checks, against a fixture database.

The bug that motivated these: `indexed_stamped` selected `from media_file`
with no alias while its WHERE clause said `mf.missing`. It raised every
single time, into a bare `contextlib.suppress`, so the check that depends on
it had never once fired. Nothing about the panel looked wrong - the row
simply was not there, and a row that is absent looks exactly like a row with
nothing to report.

So these run the real queries against a real SQLite database. A test that
mocked the connection would have passed against the broken query.
"""

from __future__ import annotations

import sqlite3

import pytest

from app import diskaudit, health
from app.config import settings
from conftest import add_track, annotate


@pytest.fixture
def wired(navidrome_db, monkeypatch):
    monkeypatch.setattr(settings, "navidrome_db", navidrome_db)
    monkeypatch.setattr(diskaudit, "_cache", {})
    return navidrome_db


UUID_JSON = '{"navidrome_uuid": [{"value": "abc-123"}]}'


def _libraries(tmp_path):
    return [{"id": 1, "name": "Music", "path": str(tmp_path / "music")}]


def _find(report, key):
    for section in report["sections"]:
        for check in section["checks"]:
            if check["key"] == key:
                return check
    return None


# --- the query that never ran ---------------------------------------------

def test_stale_index_row_appears_when_the_disk_is_ahead_of_the_index(
        wired, tmp_path, identity):
    """Regression for the query that never ran.

    Driven through `report()` on purpose. The broken version raised "no such
    column: mf.missing" into a bare suppress, leaving `indexed_stamped` None
    and the row simply absent - so anything asserting on the query in
    isolation would have passed while the panel stayed silent. This asserts
    the row is *there*, which is the thing that was actually wrong.
    """
    add_track(wired, "indexed", tags=UUID_JSON)
    add_track(wired, "not-indexed-yet", tags=None)
    # Four files carry the tag on disk; Navidrome's index knows about one,
    # because stamping preserved mtime and the incremental scan skipped them.
    diskaudit._cache[str(tmp_path / "music")] = diskaudit.Audit(
        files=4, stamped=4, taken_at=0.0)

    report = health.report(0.0, _libraries(tmp_path), identity)
    stale = _find(report, "stale_index")

    assert stale is not None, (
        "the stale-index check did not fire; indexed_stamped is None again")
    assert stale["value"] == 3
    assert stale["status"] == health.WARN


def test_stale_index_check_reports_when_disk_is_ahead_of_the_index():
    audit = diskaudit.Audit(files=10, stamped=10, taken_at=0.0)
    check = health._stale_index_check(4, audit)

    assert check is not None, (
        "six files stamped on disk but not in the index is the whole reason "
        "this check exists")
    assert check.value == 6
    assert check.status == health.WARN


def test_stale_index_check_is_silent_when_the_index_has_caught_up():
    audit = diskaudit.Audit(files=10, stamped=10, taken_at=0.0)
    assert health._stale_index_check(10, audit) is None
    assert health._stale_index_check(12, audit) is None


def test_stale_index_check_is_silent_without_an_audit():
    assert health._stale_index_check(4, None) is None


# --- scoping ---------------------------------------------------------------

def test_a_report_counts_only_the_libraries_you_can_see(wired, tmp_path,
                                                        identity):
    add_track(wired, "mine", library_id=1, tags=UUID_JSON)
    add_track(wired, "hers-1", library_id=2)
    add_track(wired, "hers-2", library_id=2)

    report = health.report(0.0, _libraries(tmp_path), identity)
    unstamped = _find(report, "unstamped")

    assert unstamped is not None
    assert unstamped["value"] == 0, (
        "kelly's two unstamped tracks are not alex's problem")


def test_a_vanished_directory_does_not_count_as_a_live_track(wired, tmp_path,
                                                             identity):
    """Navidrome marks the folder missing and leaves the rows beneath it
    untouched, so filtering on the file flag alone counts tracks that went
    months ago - and reports them as unstamped."""
    add_track(wired, "here", folder_id="f1", tags=UUID_JSON)
    add_track(wired, "gone", folder_id="gone")

    report = health.report(0.0, _libraries(tmp_path), identity)
    assert _find(report, "unstamped")["value"] == 0


def test_untaggable_formats_are_not_counted_as_missing_a_uuid(wired, tmp_path,
                                                              identity):
    """A .wav has nowhere to put the tag. Counting it leaves a red number
    that can never reach zero, which is how a panel trains you to ignore it."""
    add_track(wired, "wav", suffix="wav", tags=None)

    report = health.report(0.0, _libraries(tmp_path), identity)
    assert _find(report, "unstamped")["value"] == 0


def test_a_genuinely_unstamped_track_is_reported(wired, tmp_path, identity):
    add_track(wired, "bare", tags=None)

    report = health.report(0.0, _libraries(tmp_path), identity)
    check = _find(report, "unstamped")
    assert check["value"] == 1
    assert check["status"] == health.FAIL


def test_duplicate_uuids_are_reported(wired, tmp_path, identity):
    add_track(wired, "one", tags=UUID_JSON, path="a.mp3")
    add_track(wired, "two", tags=UUID_JSON, path="b.mp3")

    report = health.report(0.0, _libraries(tmp_path), identity)
    assert _find(report, "uuid_collisions")["value"] == 1


# --- the panel survives a database it does not recognise -------------------

def test_a_report_still_renders_when_navidrome_is_unreachable(tmp_path,
                                                              identity,
                                                              monkeypatch):
    monkeypatch.setattr(settings, "navidrome_db", tmp_path / "nothing.db")
    report = health.report(0.0, _libraries(tmp_path), identity)

    assert report["navidrome_error"]
    assert report["sections"], "the disk and system sections do not need it"
