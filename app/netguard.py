"""Which addresses the server will fetch from on a signed-in person's word.

A direct link goes to yt-dlp and a thumbnail to `covers.fetch`, both from a
URL somebody typed or a site supplied. Without a check, either reached
anything this container can: the router's admin page, Navidrome's own port,
a cloud metadata address - and the first part of the error came back to the
browser, enough to map which ports answer.

Checked by what the name resolves to, not by how it is spelled: `localhost`,
`0x7f000001` and a public name pointed at 10.0.0.1 are all the same request.
A name is resolved here and again by whatever fetches it, so a name that
changes its answer in between (DNS rebinding) can still slip past; this is
the ordinary case closed, not that one.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse


class Refused(ValueError):
    """That address is not one this fetches from."""


def check(url: str) -> None:
    """Raise Refused unless url is http(s) to a host on the public internet."""
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https"):
        raise Refused("Only http and https links can be fetched.")
    host = parts.hostname
    if not host:
        raise Refused("That link has no host.")
    try:
        found = socket.getaddrinfo(host, parts.port or None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise Refused(f"Could not find {host}.") from exc
    for *_rest, address in found:
        ip = ipaddress.ip_address(address[0].split("%", 1)[0])
        if not ip.is_global:
            raise Refused(f"{host} is on a private network; this only fetches "
                          "from the public internet.")
