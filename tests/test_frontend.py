"""The front end has no test runner, so this is what stands in for one.

Node is not available in this environment or in CI, so nothing here executes
app.js. What it does instead is check the joins - the places where a name in
one file has to match a name in another, which is where every UI bug this
project has shipped actually lived: a button wired to an id that was renamed
in the markup, a section the menu points at that no longer exists, a
stylesheet rule for an element that was deleted.

None of this proves the page works. It proves the page is wired to itself,
which used to be checked by hand on every change and therefore sometimes
was not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS_DIR = STATIC / "js"
JS_FILES = {p.name: p.read_text(encoding="utf-8") for p in sorted(JS_DIR.glob("*.js"))}
JS = "\n".join(JS_FILES.values())
CSS = (STATIC / "style.css").read_text(encoding="utf-8")

HTML_IDS = re.findall(r'id="([^"]+)"', HTML)

# Ids app.js names as string literals.
JS_IDS = set(re.findall(r'getElementById\("([^"]+)"\)', JS))
JS_IDS |= set(re.findall(r'querySelector(?:All)?\(`?"?#([A-Za-z0-9_-]+)', JS))

# Ids reached indirectly, through the table that says which panel each
# long-running operation reports into. A literal-only scan cannot see these,
# and an unresolvable one here means a finished import reports into nothing.
OPERATION_IDS = set(re.findall(r'(?:button|note): "([^"]+)"', JS))

# --- the module graph --------------------------------------------------------
# Splitting app.js into ES modules (2026-09-28) added a new kind of join this
# file didn't have to check before: an `import { x } from "./y.js"` whose `x`
# isn't exported by `y.js`, or whose path doesn't resolve. Either one fails
# the whole page silently - the browser refuses the module graph, the shell
# renders and nothing else ever runs, including the sign-in form. Nothing
# else here would catch that.

# (importer, [(name, target_path), ...]) for every named import in every file.
MODULE_IMPORTS = []
for _importer, _src in JS_FILES.items():
    for _names, _target in re.findall(r'^import \{([^}]*)\} from "([^"]+)";', _src, re.M):
        for _name in _names.split(","):
            MODULE_IMPORTS.append((_importer, _name.strip(), _target))

EXPORTED_BY = {
    name: filename for filename, src in JS_FILES.items()
    for name in re.findall(r'^export (?:async )?(?:function|const|let) ([A-Za-z0-9_$]+)', src, re.M)
}

IMPORTED_BY = {
    filename: {n.strip() for names, _t in re.findall(r'^import \{([^}]*)\} from "([^"]+)";', src, re.M)
               for n in names.split(",")}
    for filename, src in JS_FILES.items()
}


def test_no_id_appears_twice():
    """getElementById returns the first, so a duplicate silently wires half
    the page to the wrong element."""
    duplicates = sorted({i for i in HTML_IDS if HTML_IDS.count(i) > 1})
    assert duplicates == []


def test_every_id_app_js_asks_for_exists():
    assert sorted(JS_IDS - set(HTML_IDS)) == []


def test_every_panel_an_operation_reports_into_exists():
    assert sorted(OPERATION_IDS - set(HTML_IDS)) == []


def test_every_id_the_stylesheet_styles_exists():
    """A rule for an element that is gone is dead weight, and usually the
    sign of markup that was replaced rather than removed."""
    selectors = set(re.findall(r'#([A-Za-z][A-Za-z0-9_-]*)', CSS))
    # `#fff` and friends are colours, not selectors.
    selectors = {s for s in selectors
                 if not re.fullmatch(r'(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})', s)}
    assert sorted(selectors - set(HTML_IDS)) == []


def test_every_id_in_the_markup_is_used():
    """The other direction: an element nothing references is either dead or a
    rename that only got half done."""
    referenced = JS_IDS | OPERATION_IDS
    referenced |= set(re.findall(r'#([A-Za-z][A-Za-z0-9_-]*)', CSS))
    # Built by interpolation - `view-${item.dataset.view}` - and covered by
    # the menu parity test below instead.
    unused = {i for i in HTML_IDS if i not in referenced
              and not i.startswith("view-")}
    assert sorted(unused) == []


def test_every_import_path_resolves_to_a_real_module():
    """An import whose target file doesn't exist, or isn't a relative `.js`
    path, is a page that never loads - the browser refuses the whole module
    graph rather than skipping the one bad import."""
    bad = []
    for filename, src in JS_FILES.items():
        for target in re.findall(r'^import (?:\{[^}]*\}|"[^"]*") from "([^"]+)";', src, re.M):
            if not re.fullmatch(r'\./[\w-]+\.js', target):
                bad.append(f"{filename}: {target!r} is not a relative .js path")
            elif target[2:] not in JS_FILES:
                bad.append(f"{filename}: imports {target!r}, no such file")
        for target in re.findall(r'^import "([^"]+)";', src, re.M):
            if target[2:] not in JS_FILES:
                bad.append(f"{filename}: imports {target!r}, no such file")
    assert bad == []


def test_every_imported_name_is_exported_by_its_target():
    """The other half of the same join: the file exists, but does it
    actually export the name being imported?"""
    bad = [f"{importer} imports `{name}` from {target}, which does not export it"
           for importer, name, target in MODULE_IMPORTS
           if EXPORTED_BY.get(name) != target[2:]]
    assert bad == []


def test_no_file_uses_another_files_export_without_importing_it():
    """The bug class the two tests above can't see: a name exported by one
    module, used as a bare identifier in another module's body, with no
    import line pulling it in. Not a SyntaxError - it is a ReferenceError
    the first time that code path actually runs, which nothing here can
    execute to catch. (Found by hand once during the app.js module split,
    2026-09-28 - listening.js called setBanner() without importing it.)"""
    bad = []
    for filename, src in JS_FILES.items():
        # Explanatory comments in this codebase name other files' functions
        # by design (e.g. nav.js's header explains what used to call
        # loadHome/loadLibrary/loadHealth directly) - strip comments before
        # scanning for real uses, or every such comment is a false positive.
        # Block comments first, then whole-line `//` comments; this codebase
        # never trails a `//` comment after code on the same line (checked:
        # the only same-line `//` occurrences are inside string/regex
        # literals like "http://..." and /^https?:\/\//), so a whole-line-only
        # strip cannot corrupt a URL.
        code = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
        code = "\n".join(l for l in code.split("\n") if not l.strip().startswith("//"))

        # A name this file declares itself shadows an identically-named
        # export elsewhere - not a bug.
        local = set(re.findall(r'^(?:export )?(?:async )?function ([A-Za-z0-9_$]+)', code, re.M))
        local |= set(re.findall(r'^(?:export )?const ([A-Za-z0-9_$]+)', code, re.M))
        local |= set(re.findall(r'^(?:export )?let ([A-Za-z0-9_$]+)', code, re.M))
        imported = IMPORTED_BY[filename]
        for name, owner in EXPORTED_BY.items():
            if owner == filename or name in local or name in imported:
                continue
            # A bare identifier: not preceded by `.` (a property access, e.g.
            # `event.stat`) and not part of a longer identifier on either side.
            if re.search(rf'(?<![.\w$]){re.escape(name)}(?![\w$])', code):
                bad.append(f"{filename}: uses `{name}` (exported by {owner}) "
                           "without importing it")
    assert bad == []


def test_main_js_imports_library_and_health_for_their_registrations():
    """library.js (through the modules it imports) and health.js call
    registerOperation(...) at module top level - a side-effecting import.
    If main.js stopped importing one of them, its operation (replaygain/import/candidates, or audit) would never
    be registered, and starting it would silently do nothing."""
    main_js = JS_FILES["main.js"]
    assert 'import "./library.js"' in main_js or "from \"./library.js\"" in main_js
    assert 'import "./health.js"' in main_js or "from \"./health.js\"" in main_js


def test_the_menu_and_the_panels_agree():
    """showView resolves `view-${dataset.view}` for every menu button. A name
    with no section behind it resolved to null and the throw hid every panel
    at once, which is how this became a test."""
    menu = set(re.findall(r'data-view="([^"]+)"', HTML))
    panels = {i[len("view-"):] for i in HTML_IDS if i.startswith("view-")}
    assert menu == panels


@pytest.mark.parametrize("pair", ["{}", "()", "[]"])
def test_no_js_module_is_truncated(pair):
    """A crude shape check, but a JavaScript file that loses its tail takes
    the whole page down and nothing else here would notice. Checked per file
    - the whole concatenation could balance by accident even if one file's
    tail were lost and another's matched the shortfall."""
    for name, src in JS_FILES.items():
        assert src.count(pair[0]) == src.count(pair[1]), name


def test_no_top_level_function_is_declared_twice():
    """A second `function cover(...)` silently shadowed the first for every
    caller in the file - `function` redeclaration is not a SyntaxError, so
    nothing else here would have caught it. Every search-result card called
    the one and only `cover`, which by source order was the Library page's
    version, and built a Navidrome art-proxy URL out of a Spotify image URL.

    Checked per file, not across the whole module set: the same function name
    in two different ES modules is not a bug, since module scope means
    neither can shadow the other."""
    for name, src in JS_FILES.items():
        names = re.findall(r'^(?:export )?(?:async )?function ([A-Za-z0-9_$]+)\(', src, re.M)
        duplicates = sorted({n for n in names if names.count(n) > 1})
        assert duplicates == [], name


def test_the_error_banner_is_cleared_when_the_view_changes():
    """It lives outside every section, so anything left in it followed you
    onto every other panel. Asserted on the source because there is no DOM
    here: showView must clear it."""
    body = JS_FILES["nav.js"]
    body = body[body.index("function showView("):]
    body = body[:body.index("\n}\n")]
    assert 'showError("")' in body


# --- getting the new files to the browser at all ----------------------------

def _static_response(tmp_path, request_headers=()):
    from app.main import RevalidatedStatic

    asset = tmp_path / "app.js"
    # Written once. Starlette's ETag is derived from mtime and size, so
    # rewriting it between the two calls would change the ETag and the second
    # request would be asking about a different file.
    if not asset.exists():
        asset.write_text("console.log('new');", encoding="utf-8")
    static = RevalidatedStatic(directory=tmp_path)
    scope = {"type": "http", "method": "GET", "headers": list(request_headers)}
    return static.file_response(str(asset), asset.stat(), scope)


def test_static_assets_must_be_revalidated(tmp_path):
    """Starlette sends an ETag and no Cache-Control, and a response with no
    Cache-Control is heuristically cacheable - the browser picks its own
    freshness lifetime and does not ask again. That shipped a deploy where
    index.html was new (it is no-store) and app.js was months old: the
    Import as-is button was in the served file and absent from the page."""
    response = _static_response(tmp_path)
    assert response.headers["cache-control"] == "no-cache"


def test_an_unchanged_asset_still_answers_304(tmp_path):
    """`no-cache` means "ask first", not "do not store". If revalidation
    stopped returning 304 this would be a full re-download of every asset on
    every navigation, which is a different bug rather than a fix."""
    first = _static_response(tmp_path)
    etag = first.headers["etag"]

    again = _static_response(tmp_path, [(b"if-none-match", etag.encode())])
    assert again.status_code == 304
    assert again.headers["cache-control"] == "no-cache"


def test_the_shell_stamps_a_version_onto_its_assets():
    """Headers cannot rescue a browser that is already holding a stale copy:
    it does not ask, so it never learns they changed. A URL it has never seen
    is the only thing that always works."""
    import asyncio

    from app.main import asset_version, index

    html = asyncio.run(index()).body.decode()
    version = asset_version()
    for name in ("js/main.js", "style.css"):
        assert f'/static/{name}?v={version}' in html
        assert f'"/static/{name}"' not in html, "an unversioned reference left behind"


def test_the_version_follows_the_asset_contents(tmp_path, monkeypatch):
    """A token that does not change when the files do is worse than no token
    at all: it pins every browser to the stale copy permanently.

    asset_version() hashes every file under static/js, not just main.js (the
    one ASSETS stamps a URL for) - so the fixture needs a js/ subdirectory,
    and changing a *non-entry* module must still change the token."""
    from app import main

    assert main.ASSETS == ("js/main.js", "style.css")

    def version_of(main_js: str, other_js: str = "") -> str:
        js_dir = tmp_path / "js"
        js_dir.mkdir(exist_ok=True)
        (js_dir / "main.js").write_text(main_js, encoding="utf-8")
        if other_js:
            (js_dir / "other.js").write_text(other_js, encoding="utf-8")
        (tmp_path / "style.css").write_text("body{}", encoding="utf-8")
        monkeypatch.setattr(main, "STATIC_DIR", tmp_path)
        main.asset_version.cache_clear()
        return main.asset_version()

    first = version_of("console.log('one');")
    second = version_of("console.log('two');")
    assert len(first) == 12
    assert first != second

    # A change to a module main.js does not directly reference must still
    # move the token, or a browser holding a stale non-entry module is never
    # told to revalidate against a new URL.
    third = version_of("console.log('two');", other_js="console.log('a');")
    fourth = version_of("console.log('two');", other_js="console.log('b');")
    assert third != fourth
    main.asset_version.cache_clear()
    main.asset_version.cache_clear()


# --- the stylesheet's own shape ---------------------------------------------

def test_the_stylesheet_comments_are_closed():
    """An unclosed - or double-closed - comment swallows or spills whatever
    follows it, and the browser recovers silently at some later brace. Added
    after exactly that was written into this file by hand."""
    assert CSS.count("/*") == CSS.count("*/")


def test_the_stylesheet_braces_balance():
    assert CSS.count("{") == CSS.count("}")


# --- the design tokens ------------------------------------------------------
# The panels were built over months and each one decided for itself what a
# small font was, what a gap was, and what colour a quiet label should be.
# Thirteen font sizes and six weights were in use, most of them half a pixel
# from a neighbour - a difference nobody can see as a decision, and all of
# what "the formatting is all over the place" meant. These keep it from
# happening again the next time a panel is added in a hurry.

TOKENS = CSS[:CSS.index("* { box-sizing: border-box; }")]
RULES = CSS[CSS.index("* { box-sizing: border-box; }"):]
# Prose mentions px and hex constantly; only declarations count.
RULE_BODY = re.sub(r'/\*.*?\*/', '', RULES, flags=re.S)


def test_every_font_size_comes_from_the_scale():
    """Anywhere in the value, not just at the front of it: a size inside a
    clamp() or a calc() is still a size nothing else can reuse."""
    loose = [match.group(0).strip()
             for match in re.finditer(r'font-size:[^;}]*;', RULE_BODY)
             if re.search(r'(?<![-\w])[\d.]+px', match.group(0))]
    assert loose == [], "use var(--t-*), or add a step to the scale"


def test_every_font_weight_comes_from_the_scale():
    loose = re.findall(r'font-weight:\s*(\d+)', RULE_BODY)
    assert loose == [], "use var(--w-*)"


def test_spacing_comes_from_the_scale():
    """`env(safe-area-inset-*)` fallbacks are exempt: `0px` there is the
    fallback value, not a measurement anyone chose."""
    loose = []
    for match in re.finditer(r'(?:padding|margin|gap)[a-z-]*:[^;]*;', RULE_BODY):
        declaration = match.group(0)
        if "env(safe-area-inset" in declaration:
            continue
        if re.search(r'(?<![-\w])\d+px', declaration.split(":", 1)[1]):
            loose.append(declaration.strip())
    assert loose == [], "use var(--s-*)"


def test_colours_are_named_in_one_place():
    """A hex outside :root is a colour nothing else can reuse and nothing
    names. #fff and #000 are allowed: they are mixing endpoints in
    color-mix(), not colours in their own right."""
    # Not followed by a hyphen or another word character, or `#add-form`
    # reads as a colour - "add" is three hex digits.
    loose = [h for h in re.findall(r'#[0-9a-fA-F]{3,8}(?![-\w])', RULE_BODY)
             if h.lower() not in ("#fff", "#000")]
    assert loose == [], "add it to :root and use var(--name)"


def test_fields_are_large_enough_not_to_zoom_ios():
    """Below 16px, iOS Safari zooms the page when a field takes focus and
    does not zoom back. The user reads this on a phone."""
    assert "--t-field: 16px" in TOKENS
    field_rule = RULE_BODY[RULE_BODY.index("input:not([type=\"radio\"])"):]
    field_rule = field_rule[:field_rule.index("}")]
    assert "var(--t-field)" in field_rule


# --- the playlist editor's operator names ------------------------------------
# A substring test for "InTheLast" never matched "inTheLast", so "in the last"
# showed a date picker and saved a date as its day count (CODE_REVIEW H8).
# The names the editor tests for must be names the server offers.

def test_the_day_count_operators_are_the_servers_date_operators():
    from app import playlists

    found = re.search(r"const DAY_OPERATORS = \[([^\]]*)\]", JS_FILES["playlists.js"])
    assert found, "playlists.js no longer declares DAY_OPERATORS"
    named = set(re.findall(r'"([^"]+)"', found.group(1)))
    offered = {o["name"] for o in playlists.OPERATORS["date"]}

    assert named <= offered, f"not offered by the server: {named - offered}"
    assert named == {name for name in offered if "inthelast" in name.lower()}


def test_the_editor_does_not_match_operators_by_substring():
    assert not re.search(r"operator(?:\.value)?\.indexOf\(", JS_FILES["playlists.js"])


# --- the Listening range ------------------------------------------------------
# The default range was written in three places, and the selection was a
# class only (CODE_REVIEW M6). The page now marks it itself, so the markup
# carries none, and the server warms the cache for the same default.

def test_the_default_listening_range_is_the_one_the_server_warms():
    from app import overview

    found = re.search(r"let listeningDays = (\d+);", JS_FILES["listening.js"])
    assert found and int(found.group(1)) == overview.OPENING_DAYS
    assert not re.search(r'class="range[^"]*\bactive\b', HTML)


def test_the_selected_range_is_announced_not_only_coloured():
    assert 'setAttribute("aria-pressed"' in JS_FILES["listening.js"]


def test_the_cover_survey_is_fetched_from_one_place():
    """Two callers fetching it at once ran the survey twice (CODE_REVIEW M38)."""
    assert JS.count('"/api/library/attention/covers"') == 1


# --- which library new music goes into (L11) ---------------------------------
# Neither Browse nor Drop sent a library, so an account with two could never
# put anything into its second.

def test_a_download_says_which_library():
    assert "library_id: targetLibrary()" in JS_FILES["browse.js"]


def test_an_upload_and_its_finish_say_which_library():
    drop = JS_FILES["drop.js"]
    assert 'body.append("library_id"' in drop
    assert "&library_id=" in drop


def test_both_pages_offer_the_choice():
    for picker in ("browse-library", "drop-library"):
        assert re.search(rf'<label class="target-library" hidden>[^<]*<select id="{picker}"', HTML)
        assert f'getElementById("{picker}")' in JS


# --- what the Drop file picker offers (L12) -----------------------------------

def _js_set(name):
    found = re.search(rf"const {name} = new Set\(\[(.*?)\]\);", JS_FILES["drop.js"], re.S)
    return set(re.findall(r'"(\.[a-z0-9]+)"', found.group(1)))


def test_the_drop_lists_match_what_the_server_files():
    from app import filer, uuidtags

    assert _js_set("DROP_AUDIO_EXT") == uuidtags.AUDIO_SUFFIXES
    assert _js_set("DROP_COVER_EXT") == set(filer.COVER_SUFFIXES)


def test_the_file_picker_offers_covers_and_every_audio_extension():
    """accept="audio/*" alone hid covers, and .ape/.wv on some systems."""
    assert 'accept="audio/*"' not in HTML
    assert ('dropInput.accept = ["audio/*", ...DROP_AUDIO_EXT, ...DROP_COVER_EXT]'
            in JS_FILES["drop.js"])


def test_an_artist_page_reads_every_record_not_one_page():
    """It asked for one page of 200 and stopped there without a word (L24)."""
    assert "const want = viewing.artist ? Infinity" in JS_FILES["library.js"]


def test_the_year_tile_opens_its_own_calendar_year():
    """It counted the calendar year and opened the last 365 days (L29)."""
    assert "selectDates(`${shown}-01-01`, `${shown}-12-31`)" in JS_FILES["home.js"]
    assert "selectRange(365)" not in JS_FILES["home.js"]


def test_an_empty_monthly_chart_does_not_claim_a_peak_of_one():
    """The scale's floor of 1 was also the label (L31)."""
    charts = JS_FILES["charts.js"]
    assert 'if (peak) plot.append(el("span", "chart-peak-label"' in charts
    assert '"chart-peak-label", most' not in charts


def test_focus_shows_in_forced_colours():
    """Every focus ring is a box-shadow, which forced colours drops (L45)."""
    assert "@media (forced-colors: active)" in CSS
    assert re.search(r"forced-colors: active\) \{\s*:focus-visible \{ outline: 2px solid CanvasText", CSS)


def test_operations_are_caught_up_on_every_socket_open():
    """An outcome is announced once; one that finished while the socket was
    down left a progress line and disabled buttons until a reload (2H3).
    Checked in Chromium with a stubbed socket; this pins the wiring."""
    assert "onOpen?.();" in JS_FILES["ws.js"]
    assert "connect(onSessionExpired, onOpen)" in JS_FILES["ws.js"]
    assert "connect(handleSessionExpired, resumeOperations)" in JS_FILES["main.js"]
    assert "watching.has(op.name)" in JS_FILES["operations.js"]


def test_every_library_loader_shares_one_latest_load():
    """M5's counter covered albums against albums only, so a slow albums
    answer drew the grid under the Artists or Needs attention tab, and the
    reverse (2M21). Checked in Chromium; this pins the wiring."""
    assert "albumsRequest" not in JS
    assert JS_FILES["library.js"].count("const mine = ++libraryState.load;") == 2
    assert "const mine = ++libraryState.load;" in JS_FILES["library-attention.js"]


def test_a_401_from_any_api_call_shows_the_sign_in_form():
    """Only the socket's 4401 led to sign-in; a session that ended while the
    socket stayed open left every panel saying "Please sign in." (2M22).
    Checked in Chromium; this pins the wiring."""
    raw = [name for name, text in JS_FILES.items()
           if re.search(r"(?<![\w.])fetch\(", text) and name not in ("core.js", "main.js")]
    assert raw == []
    assert "if (response.status === 401 && signedOut) signedOut();" in JS_FILES["core.js"]
    assert "whenSignedOut(handleSessionExpired);" in JS_FILES["main.js"]


def test_an_operation_that_did_not_start_is_not_treated_as_started():
    """Combine closed its dialog and cleared the selection, and Use this
    closed the drawer, when the server answered started: false (2M23).
    Checked in Chromium; this pins the wiring."""
    assert "if (!payload.started) {" in JS_FILES["library-combine.js"]
    assert "if (!payload || payload.detail || !payload.started) {" in JS_FILES["library-drawer.js"]


def test_a_drop_goes_to_the_library_chosen_when_it_was_dropped():
    """targetLibrary() was read per file and again at finish, so switching
    the picker mid-upload split one drop across two libraries (2M24).
    Checked in Chromium; this pins the wiring."""
    drop = JS_FILES["drop.js"]
    assert drop.count("targetLibrary()") == 1
    assert "dropRunBatch(items, rows, card, skipped, library)" in drop


def test_escape_belongs_to_a_text_box_and_to_the_menu_first():
    """Escape in the search box wiped the selection, in the merge box closed
    the editor, and closing the menu also closed the panel behind (2M25).
    Checked in Chromium; this pins the wiring."""
    for name in ("library.js", "browse.js", "downloads.js"):
        assert "pageEscape(event)" in JS_FILES[name], name
    assert 'event.key === "Escape"' not in JS_FILES["library.js"] + JS_FILES["browse.js"]
    assert "}, true);" in JS_FILES["nav.js"]


def test_inline_track_saves_run_in_order_and_undo_a_refusal():
    """A save refused with 409 left its text in the field under a later
    "Saved.", and quick edits overlapped into that 409 (2M26). Checked in
    Chromium; this pins the wiring."""
    drawer = JS_FILES["library-drawer.js"]
    assert "trackEdits = trackEdits.then(" in drawer
    assert 'input.value = previous || "";' in drawer


def test_a_squared_cover_is_stamped_after_the_rescan_not_before():
    """Bulk Square covers stamped the art URL at once, so it was fetched
    while Navidrome still served the barred picture, and cached for a week.
    The listing's art_version makes a new cover survive a reload (2M28)."""
    attention = JS_FILES["library-attention.js"]
    square = attention[attention.index("export async function squareCovers"):]
    square = square[:square.index("\n}\n")]
    assert "RESCAN_WAIT_MS" in square
    assert "album.art_version" in JS_FILES["library-shared.js"]


def test_the_combine_dialog_keeps_its_focus_across_rebuilds():
    """Spotify's guess arriving mid-word took the rest of the typing with it,
    a keyboard move threw focus to the page, and the dialog neither took
    focus nor held it (2M29). Checked in Chromium; this pins the wiring."""
    combine = JS_FILES["library-combine.js"]
    assert "restoreFocus(sheet, held, caret);" in combine
    assert "up.dataset.focus = `up:${item.track.path}`;" in combine
    assert 'libraryDialog.addEventListener("keydown"' in combine


def test_a_rebuilt_retry_button_stays_pressed():
    """The job's rows are rebuilt on every message, which handed back a
    fresh, enabled Retry while the first press was in flight (2L1)."""
    downloads = JS_FILES["downloads.js"]
    assert "retry.disabled = retrying.has(job.id);" in downloads
    assert "if (retrying.has(id)) return;" in downloads


def test_home_says_when_the_history_could_not_be_read():
    """Zeros on their own read as "you have never played anything" (2L17)."""
    assert "heard.available === false" in JS_FILES["home.js"]


def test_duplicates_says_a_failure_is_a_failure():
    """A 503 on the preview read as "Nothing is confident enough", and a 500
    on the set-aside list as "Nothing has been set aside" (2L20). Checked in
    Chromium; this pins the wiring."""
    dupes = JS_FILES["duplicates.js"]
    assert 'await getJSON("/api/duplicates/quarantined")' in dupes
    assert 'await postJSON("/api/duplicates/auto", {})' in dupes
    assert "export async function getJSON" in JS_FILES["core.js"]


def test_signing_in_again_on_the_same_page_starts_clean():
    """After a session ended, the next person to sign in on that page saw
    the first account's cached Artists list (2L21). Checked in Chromium."""
    main = JS_FILES["main.js"]
    assert "signedOutHere = true;" in main
    assert "if (signedOutHere) {" in main and "location.reload();" in main


def test_any_library_change_drops_the_artists_list():
    """Only Rescan and a combine dropped it, so a renamed artist or a new
    download stayed out of the Artists tab for the life of the page (2L22)."""
    library = JS_FILES["library.js"]
    refresh = library[library.index("export function refreshLibrary"):]
    assert "libraryState.artistsCache = null;" in refresh[:refresh.index("\n}\n")]


def test_quarantine_waits_for_a_stub_albums_tracks():
    """It threw before asking anything when opened from a song search (2L23).
    Checked in Chromium; this pins the wiring."""
    drawer = JS_FILES["library-drawer.js"]
    body = drawer[drawer.index("async function quarantineAlbum"):]
    assert body.index("album.tracks === undefined") < body.index("confirm(")


def test_the_selection_follows_what_moved():
    """A renamed or quarantined album stayed selected under its old folder,
    and a renamed track kept its old key (2L24)."""
    drawer = JS_FILES["library-drawer.js"]
    assert "function forgetPicks(album, renamed = null)" in drawer
    assert drawer.count("forgetPicks(album") >= 2
    assert "selection.tracks.set(track.path, picked);" in drawer


def test_a_survey_in_flight_cannot_undo_an_invalidation():
    """A survey that answered after a cover was squared was stored over the
    invalidation, and the Cover flag came back on the album just fixed
    (2L25)."""
    shared = JS_FILES["library-shared.js"]
    assert "if (asked !== libraryState.surveyGeneration) return survey;" in shared
    assert "libraryState.coverSurvey = null;" not in JS.replace(
        shared[shared.index("export function forgetCoverSurvey"):], "")
