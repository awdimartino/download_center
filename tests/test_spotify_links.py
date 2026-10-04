"""Every form a Spotify link is pasted in reaches Spotify (L1).

Links without the scheme and spotify: URIs were searched by the page instead
of queued, embed links were refused, and spotify.link short links went to
yt-dlp, which cannot read them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import generic, main, spotify

BROWSE = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
          / "browse.js").read_text(encoding="utf-8")

FORMS = [
    "https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj",
    "https://open.spotify.com/intl-de/album/4yP0hdKOZPNshxUOjY0cZj?si=abc",
    "open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj",
    "spotify:album:4yP0hdKOZPNshxUOjY0cZj",
    "https://open.spotify.com/embed/album/4yP0hdKOZPNshxUOjY0cZj",
]


@pytest.mark.parametrize("link", FORMS)
def test_every_form_parses_to_the_same_album(link):
    assert spotify.parse_link(link) == ("album", "4yP0hdKOZPNshxUOjY0cZj")
    assert spotify.canonical(link) == \
        "https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj"
    main.validate(link)


@pytest.mark.parametrize("link", ["https://spotify.link/AbC123xyz",
                                  "spotify.link/AbC123xyz"])
def test_a_short_link_goes_to_spotify_not_yt_dlp(link, monkeypatch):
    monkeypatch.setattr(generic, "resolve",
                        lambda url: pytest.fail("handed to yt-dlp"))
    followed = []
    monkeypatch.setattr(spotify, "expand_short", lambda text: followed.append(text)
                        or "https://open.spotify.com/track/abc")
    monkeypatch.setattr(spotify, "resolve",
                        lambda kind, sid: ("A song", [{"id": sid, "kind": kind}]))
    main.validate(link)
    assert main._resolve(link) == ("track", "A song", [{"id": "abc", "kind": "track"}])
    assert followed == [link]


def test_a_short_link_is_followed_to_where_it_redirects(monkeypatch):
    class Response:
        url = "https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6?si=x"
        text = ""

    seen = {}

    def get(url, **kwargs):
        seen["url"] = url
        return Response()

    monkeypatch.setattr(spotify.requests, "get", get)
    assert spotify.expand_short("spotify.link/AbC") == \
        "https://open.spotify.com/track/6rqhFgbbKwnb9MLmUQDhG6"
    assert seen["url"] == "https://spotify.link/AbC"


def test_a_short_link_that_leads_nowhere_says_so(monkeypatch):
    class Response:
        url = "https://spotify.link/AbC"
        text = "<html>Not found</html>"

    monkeypatch.setattr(spotify.requests, "get", lambda url, **kw: Response())
    with pytest.raises(spotify.ResolveError):
        spotify.expand_short("https://spotify.link/AbC")


def test_other_sites_still_go_to_yt_dlp():
    assert not spotify.is_spotify("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    main.validate("https://www.youtube.com/watch?v=dQw4w9WgXcQ")


def _page_test():
    body = re.search(r"function looksLikeUrl\(text\) \{\s*return (/.*?/)i\s*\.test",
                     BROWSE, re.S).group(1)
    return re.compile(body[1:-1].replace("\\/", "/"), re.I)


@pytest.mark.parametrize("link", FORMS + ["https://spotify.link/AbC",
                                          "spotify.link/AbC",
                                          "https://youtu.be/dQw4w9WgXcQ"])
def test_the_page_queues_every_form_instead_of_searching(link):
    assert _page_test().match(link)


@pytest.mark.parametrize("text", ["thriller", "spotify", "michael jackson bad"])
def test_the_page_still_searches_words(text):
    assert not _page_test().match(text)
