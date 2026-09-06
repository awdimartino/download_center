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


# --- scoping and counting details -----------------------------------------

def test_orphan_annotations_are_this_persons_only(wired, tmp_path, identity):
    """It used to count every user's, so a non-admin saw a number they could
    neither explain nor act on."""
    annotate(wired, "u-alex", "vanished-1", starred=1)
    annotate(wired, "u-kelly", "vanished-2", starred=1)
    annotate(wired, "u-kelly", "vanished-3", starred=1)

    report = health.report(0.0, _libraries(tmp_path), identity)
    assert _find(report, "orphan_annotations")["value"] == 1


def test_a_zero_db_replaygain_counts_as_measured(wired, tmp_path, identity):
    """0 dB is exactly what an already-normalised track measures at. Testing
    `!= 0` reported it as never measured."""
    add_track(wired, "normalised", rg_track_gain=0.0, tags=UUID_JSON)
    add_track(wired, "quiet", rg_track_gain=-6.2, tags=UUID_JSON)
    add_track(wired, "unmeasured", rg_track_gain=None, tags=UUID_JSON)

    report = health.report(0.0, _libraries(tmp_path), identity)
    assert _find(report, "no_replaygain")["value"] == 1


# --- the cut-down panel -----------------------------------------------------

def _keys(report):
    return {c["key"] for s in report["sections"] for c in s["checks"]}


def _check(report, key):
    for section in report["sections"]:
        for check in section["checks"]:
            if check["key"] == key:
                return check
    return None


def test_the_rows_nobody_acted_on_are_gone(wired, tmp_path, identity):
    """Informational, never acted on, and between them they taught you that
    a non-zero number here means nothing."""
    add_track(wired, "t1", album="", track_number=0, tags=UUID_JSON)

    report = health.report(0.0, _libraries(tmp_path), identity)
    assert "no_album" not in _keys(report)
    assert "no_track_number" not in _keys(report)


def test_the_staging_summary_is_gone(wired, tmp_path, identity):
    """The Staging tab shows the same thing in full, one tap away."""
    report = health.report(0.0, _libraries(tmp_path), identity)
    keys = _keys(report)
    assert "staging_albums" not in keys
    assert "staging_singles" not in keys
    assert "staging_age" not in keys


def test_the_uuid_question_is_asked_once_not_twice(wired, tmp_path, identity):
    """The database row and the disk row were the same question. So were the
    two duplicate-UUID rows."""
    add_track(wired, "t1", tags=UUID_JSON)
    diskaudit._cache[str(tmp_path / "music")] = diskaudit.Audit(
        files=1, stamped=1, taken_at=0.0)

    report = health.report(0.0, _libraries(tmp_path), identity)
    keys = _keys(report)
    assert "unstamped" in keys and "uuid_collisions" in keys
    assert "disk_unstamped" not in keys
    assert "disk_duplicate_uuids" not in keys


def test_the_disk_wins_when_it_disagrees_with_the_index(wired, tmp_path,
                                                        identity):
    """Navidrome's index can be stale; the files cannot. A file stamped but
    not yet re-read is not an unstamped file, and calling it one sends you
    looking for work already done."""
    add_track(wired, "a", tags=None, path="a.mp3")
    add_track(wired, "b", tags=None, path="b.mp3")
    diskaudit._cache[str(tmp_path / "music")] = diskaudit.Audit(
        files=2, stamped=2, missing_track_uuid=[], taken_at=0.0)

    report = health.report(0.0, _libraries(tmp_path), identity)
    check = _check(report, "unstamped")
    assert check["value"] == 0, "the index says two, the disk says none"
    assert "files themselves" in check["detail"]


def test_migration_artefacts_are_demoted_not_deleted(wired, tmp_path, identity):
    """They can recur, rarely. A row nobody reads still beats a number
    nobody can get at when it finally matters."""
    diskaudit._cache[str(tmp_path / "music")] = diskaudit.Audit(
        files=1, stamped=1, split_albums=["Artist/Album"],
        spanning_albums=["uuid in 2 directories"], taken_at=0.0)

    report = health.report(0.0, _libraries(tmp_path), identity)
    for key in ("disk_split_albums", "disk_spanning_albums", "disk_files"):
        assert _check(report, key)["secondary"] is True, key


def test_status_facts_are_demoted(wired, tmp_path, identity):
    report = health.report(0.0, _libraries(tmp_path), identity)
    assert _check(report, "uptime")["secondary"] is True
    assert _check(report, "last_scan")["secondary"] is True


def test_free_space_stays_on_the_front(wired, tmp_path, identity):
    """A fact, but one you act on."""
    report = health.report(0.0, _libraries(tmp_path), identity)
    assert _check(report, "disk_free")["secondary"] is False


def test_the_badge_counts_only_what_can_be_acted_on(wired, tmp_path, identity):
    """Every row is shown, but the status rows are not problems. A badge
    counting those sends you looking for something that is not wrong."""
    add_track(wired, "bare", tags=None)
    diskaudit._cache[str(tmp_path / "music")] = diskaudit.Audit(
        files=1, stamped=0, missing_track_uuid=["bare.mp3"],
        split_albums=["a", "b"], taken_at=0.0)

    report = health.report(0.0, _libraries(tmp_path), identity)
    shown = [c for s in report["sections"] for c in s["checks"]
             if not c["secondary"] and c["status"] in (health.WARN, health.FAIL)]
    assert report["problems"] == len(shown)
    assert "hidden" not in report, "the toggle it counted for is gone"


def test_the_panel_is_about_a_dozen_rows_not_twenty(wired, tmp_path, identity):
    """The point of the exercise: twenty-one checks in six sections was too
    many to read, so nothing in it got read."""
    add_track(wired, "t1", tags=UUID_JSON)
    report = health.report(0.0, _libraries(tmp_path), identity)
    primary = [c for s in report["sections"] for c in s["checks"]
               if not c["secondary"]]
    assert len(primary) <= 12, [c["key"] for c in primary]
