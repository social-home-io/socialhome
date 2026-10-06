"""``normalized_peer_url`` — a GFS cluster peer's base URL (spec §24.10).

The URL is stored on the peer's ``cluster_nodes`` row, POSTed to on every
fan-out, and echoed on the public ``GET /cluster/health``. So it must be a
plain base URL, re-serialised from its parts — never a string that smuggles
a header line, a NUL, whitespace or a deceptive (bidi) character through.
"""

from __future__ import annotations

import pytest

from socialhome.global_server.peer_url import normalized_peer_url


@pytest.mark.parametrize(
    "raw, want",
    [
        ("https://b.gfs.test", "https://b.gfs.test"),
        ("https://b.gfs.test/", "https://b.gfs.test"),
        ("HTTP://B.GFS.Test:8080/", "http://b.gfs.test:8080"),
        ("http://127.0.0.1:9000/base/", "http://127.0.0.1:9000/base"),
        ("http://[::1]:8080", "http://[::1]:8080"),
        ("http://gfs_node-0:8000", "http://gfs_node-0:8000"),
        # Surrounding whitespace is a paste artefact, stripped.
        ("  https://b.gfs.test \n", "https://b.gfs.test"),
        # A non-ASCII host is IDNA-encoded.
        ("http://bücher.example", "http://xn--bcher-kva.example"),
    ],
)
def test_valid_urls_are_re_serialised(raw, want):
    assert normalized_peer_url(raw) == want


@pytest.mark.security
@pytest.mark.parametrize(
    "raw",
    [
        # Control characters urlsplit silently strips or keeps.
        "http://ex\nample.com",
        "http://h:80\r\nX-Inj: 1",
        "http://h/\x00",
        "http://h/\x7f",
        "http://q.test/‮evil\r\nX: 1",
        # Whitespace inside.
        "http://a b.com",
        "http://h/a b",
        "http://h/\tx",
        # Bidi / invisible formatting characters.
        "http://‮evil.com",
        "http://ex​ample.com",
        "http://h/⁦x",
        # Non-ASCII outside the host.
        "http://h/pfad-ä",
        # A host IDNA cannot encode.
        "http://" + "a" * 64 + ".com",
        "http://xn--.com",
        # Ports: out of range, zero, not a number.
        "http://h:99999",
        "http://h:0",
        "http://h:-1",
        "http://h:abc",
        # Characters no hostname has.
        "http://h%0d%0a.com",
        "http://h<x>.com",
    ],
)
def test_unsafe_urls_are_refused(raw):
    assert normalized_peer_url(raw) == ""


@pytest.mark.parametrize(
    "raw",
    [
        None,
        42,
        "",
        "ftp://c.gfs.test",
        "http://",
        "http://user:pw@c.gfs.test",
        "javascript:alert(1)",
        "https://c.gfs.test/?q=1",
        "https://c.gfs.test/#frag",
        "c.gfs.test",
    ],
)
def test_non_base_urls_are_refused(raw):
    assert normalized_peer_url(raw) == ""


def test_the_result_is_printable_ascii():
    out = normalized_peer_url("HTTP://Bücher.Example:8443/x/")
    assert out == "http://xn--bcher-kva.example:8443/x"
    assert out.isascii() and out.isprintable()
