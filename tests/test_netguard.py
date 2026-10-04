"""The server fetches only from the public internet (CODE_REVIEW L42).

A direct link or a thumbnail could name any address the container can
reach - the router, Navidrome's port, a metadata service - and the first
200 characters of the error came back, enough to map what answers.
"""

from __future__ import annotations

import socket

import pytest

from app import generic, main, netguard


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:4533/rest/ping",
    "http://localhost:8000/api/settings",
    "http://192.168.1.1/",
    "http://10.0.0.5/",
    "http://172.18.0.4:4533/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://0x7f000001/",
    "file:///etc/passwd",
    "ftp://example.com/x",
])
def test_a_private_or_odd_address_is_refused(url):
    with pytest.raises(netguard.Refused):
        netguard.check(url)


def test_a_name_that_resolves_to_a_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))])
    with pytest.raises(netguard.Refused):
        netguard.check("https://innocent.example/watch?v=x")


def test_a_public_address_is_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.80.46", 0))])
    netguard.check("https://www.youtube.com/watch?v=x")


def test_queuing_a_direct_link_to_the_lan_is_refused():
    with pytest.raises(generic.ResolveError, match="private network"):
        main.validate("http://192.168.1.1/admin")
