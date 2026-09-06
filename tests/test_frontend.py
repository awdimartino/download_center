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
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")

HTML_IDS = re.findall(r'id="([^"]+)"', HTML)

# Ids app.js names as string literals.
JS_IDS = set(re.findall(r'getElementById\("([^"]+)"\)', JS))
JS_IDS |= set(re.findall(r'querySelector(?:All)?\(`?"?#([A-Za-z0-9_-]+)', JS))

# Ids reached indirectly, through the table that says which panel each
# long-running operation reports into. A literal-only scan cannot see these,
# and an unresolvable one here means a finished import reports into nothing.
OPERATION_IDS = set(re.findall(r'(?:button|note): "([^"]+)"', JS))


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


def test_the_menu_and_the_panels_agree():
    """showView resolves `view-${dataset.view}` for every menu button. A name
    with no section behind it resolved to null and the throw hid every panel
    at once, which is how this became a test."""
    menu = set(re.findall(r'data-view="([^"]+)"', HTML))
    panels = {i[len("view-"):] for i in HTML_IDS if i.startswith("view-")}
    assert menu == panels


@pytest.mark.parametrize("pair", ["{}", "()", "[]"])
def test_app_js_is_not_truncated(pair):
    """A crude shape check, but a JavaScript file that loses its tail takes
    the whole page down and nothing else here would notice."""
    assert JS.count(pair[0]) == JS.count(pair[1])


def test_the_error_banner_is_cleared_when_the_view_changes():
    """It lives outside every section, so anything left in it followed you
    onto every other panel. Asserted on the source because there is no DOM
    here: showView must clear it."""
    body = JS[JS.index("function showView("):]
    body = body[:body.index("\n}\n")]
    assert 'showError("")' in body
