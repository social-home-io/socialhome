"""Unit tests for :mod:`socialhome.services.invite_link_service`.

No network: the GFS session is a double; the address vetting runs for real
(``open_pinned_session`` with a stub resolver), so a name that resolves into
a private range is refused before any connection.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import orjson
import pytest

from socialhome.domain.federation import GfsConnection
from socialhome.outbound_fetch import open_pinned_session
from socialhome.services import invite_link_service as mod
from socialhome.services.invite_link_service import (
    InviteLinkExpiredError,
    InviteLinkRefusedError,
    InviteLinkService,
    InviteLinkUnreachableError,
    parse_invite_link,
)

CODE = "socialhome://invite#eyJ0b2tlbiI6ICJhYmMifQ"


class _Stream:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, n: int = -1) -> bytes:
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk


class _Resp:
    def __init__(self, status: int, body: bytes, declared: int | None) -> None:
        self.status = status
        self.content = _Stream(body)
        self.content_length = declared


class _Session:
    """Answers every POST with one canned response; records the calls."""

    def __init__(self, status=200, body=None, *, declared=None, delay=0.0):
        if body is None:
            body = orjson.dumps({"server_name": "G", "invite_code": CODE})
        self._resp = _Resp(status, body, declared)
        self._delay = delay
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    @asynccontextmanager
    async def _post(self, url, **kw):
        self.calls.append((url, kw))
        if self._delay:
            await asyncio.sleep(self._delay)
        yield self._resp

    def post(self, url, **kw):
        return self._post(url, **kw)

    async def close(self) -> None:
        self.closed = True


class _Gfs:
    """The two :class:`GfsConnectionService` calls the resolver uses."""

    def __init__(self, conns=(), session=None) -> None:
        self._conns = list(conns)
        self._session = session

    async def list_connections(self):
        return self._conns

    @asynccontextmanager
    async def anon_session(self, conn):
        yield self._session


def _conn(url: str) -> GfsConnection:
    return GfsConnection(
        id="g",
        gfs_instance_id="g",
        display_name="G",
        public_key="aa" * 32,
        inbox_url=url,
        status="active",
        paired_at="2026-11-01T00:00:00+00:00",
    )


def _svc(session: _Session | None = None, *, gfs: _Gfs | None = None):
    opened: list[str] = []

    async def open_session(url: str):
        opened.append(url)
        return session

    svc = InviteLinkService(gfs or _Gfs(), open_session=open_session)  # type: ignore[arg-type]
    return svc, opened


# ─── parse_invite_link ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("link", "want"),
    [
        ("https://gfs.example.org/join/tok", ("https://gfs.example.org", "tok")),
        (
            "HTTPS://gfs.example.org:8443/join/a-b_c.d~e",
            ("https://gfs.example.org:8443", "a-b_c.d~e"),
        ),
        (
            "https://gfs.example.org/base/join/tok",
            ("https://gfs.example.org/base", "tok"),
        ),
        ("http://192.168.1.5:8765/join/tok", ("http://192.168.1.5:8765", "tok")),
    ],
)
def test_parse_accepts_join_links(link, want):
    assert parse_invite_link(link) == want


@pytest.mark.parametrize(
    "link",
    [
        None,
        "",
        "x" * 3000,
        "https://gfs.example.org/join/tok\n",
        "https://gfs.example.org/join/t%20k",
        "https://gfs.example.org:99999/join/tok",
        "https://[::1/join/tok",
        "javascript:alert(1)//join/tok",
        "https:///join/tok",
        "https://u@gfs.example.org/join/tok",
        "https://gfs.example.org/join/tok?",
        "https://gfs.example.org/join/tok#",
        "https://gfs.example.org/join/tok/",
        "https://gfs.example.org/join/...",
        "https://gfs.example.org/join/" + "a" * 257,
    ],
)
def test_parse_refuses_anything_else(link):
    with pytest.raises(InviteLinkRefusedError):
        parse_invite_link(link)


# ─── resolve: a GFS we are not connected to ───────────────────────────────


async def test_public_https_gfs_is_asked_over_a_pinned_session():
    session = _Session()
    svc, opened = _svc(session)
    assert await svc.resolve("https://gfs.example.org/join/tok") == CODE
    assert opened == ["https://gfs.example.org"]
    url, kw = session.calls[0]
    assert url == "https://gfs.example.org/gfs/pages/invite"
    assert orjson.loads(kw["data"]) == {"token": "tok"}
    assert kw["allow_redirects"] is False
    assert session.closed


async def test_dead_invite_raises_expired():
    svc, _ = _svc(_Session(404, b'{"server_name":"G"}'))
    with pytest.raises(InviteLinkExpiredError):
        await svc.resolve("https://gfs.example.org/join/tok")


@pytest.mark.parametrize(
    ("status", "body", "declared"),
    [
        (500, b"{}", None),
        (302, b"", None),
        (200, b"not json", None),
        (200, b'["socialhome://invite#x"]', None),
        (200, b'{"invite_code": "https://evil.example"}', None),
        (200, orjson.dumps({"invite_code": "socialhome://invite#" + "A" * 9000}), None),
        (200, b"{}", mod.MAX_ANSWER_BYTES + 1),
        (200, b"{" + b" " * (mod.MAX_ANSWER_BYTES + 10) + b"}", None),
    ],
)
async def test_anything_but_an_invite_code_is_unreachable(status, body, declared):
    svc, _ = _svc(_Session(status, body, declared=declared))
    with pytest.raises(InviteLinkUnreachableError):
        await svc.resolve("https://gfs.example.org/join/tok")


async def test_one_deadline_covers_the_exchange(monkeypatch):
    monkeypatch.setattr(mod, "RESOLVE_TIMEOUT_S", 0.05)
    svc, _ = _svc(_Session(delay=1.0))
    with pytest.raises(InviteLinkUnreachableError):
        await svc.resolve("https://gfs.example.org/join/tok")


async def test_plain_http_is_refused_unless_connected():
    svc, opened = _svc(_Session())
    with pytest.raises(InviteLinkRefusedError):
        await svc.resolve("http://gfs.example.org/join/tok")
    assert opened == []


@pytest.mark.parametrize(
    "answer", ["10.0.0.7", "127.0.0.1", "169.254.169.254", "fd00::1"]
)
async def test_a_name_resolving_into_a_private_range_is_refused(answer):
    """The real pinned-session vetting: DNS says private → no connection."""

    async def resolver(host: str, port: int) -> list[str]:
        return [answer]

    async def open_session(url: str):
        return await open_pinned_session(url, resolver=resolver)

    svc = InviteLinkService(_Gfs(), open_session=open_session)  # type: ignore[arg-type]
    with pytest.raises(InviteLinkRefusedError):
        await svc.resolve("https://gfs.example.org/join/tok")


async def test_a_name_that_does_not_resolve_is_unreachable():
    async def resolver(host: str, port: int) -> list[str]:
        raise OSError("nxdomain")

    async def open_session(url: str):
        return await open_pinned_session(url, resolver=resolver)

    svc = InviteLinkService(_Gfs(), open_session=open_session)  # type: ignore[arg-type]
    with pytest.raises(InviteLinkUnreachableError):
        await svc.resolve("https://gfs.example.org/join/tok")


# ─── resolve: a GFS this household is connected to ────────────────────────


async def test_connected_gfs_uses_the_household_session_even_on_the_lan():
    shared = _Session()
    svc, opened = _svc(gfs=_Gfs([_conn("http://192.168.1.5:8765/")], shared))
    assert await svc.resolve("http://192.168.1.5:8765/join/tok") == CODE
    assert opened == []  # never the pinned (public-only) session
    assert shared.calls[0][0] == "http://192.168.1.5:8765/gfs/pages/invite"
    assert not shared.closed  # the household's own session stays open


async def test_connection_match_is_exact_not_a_prefix():
    svc, opened = _svc(_Session(), gfs=_Gfs([_conn("http://192.168.1.5:8765")], None))
    with pytest.raises(InviteLinkRefusedError):
        await svc.resolve("http://192.168.1.5:8766/join/tok")
    assert opened == []


async def test_connected_gfs_without_a_session_is_unreachable():
    svc, _ = _svc(gfs=_Gfs([_conn("https://gfs.example.org")], None))
    with pytest.raises(InviteLinkUnreachableError):
        await svc.resolve("https://gfs.example.org/join/tok")
