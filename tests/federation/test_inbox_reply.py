"""The sender's reading of the inbox's "already held" answer."""

from __future__ import annotations

import pytest

from socialhome.federation.inbox_reply import (
    INBOX_CODE_REPLAY,
    INBOX_REPLAY_STATUS,
    inbox_holds_envelope,
)


class _Stream:
    """A response's ``content``: ``read(n)`` hands out at most ``n`` bytes."""

    def __init__(self, body) -> None:
        self._body = body
        self.asked: list[int] = []

    async def read(self, n: int = -1) -> bytes:
        self.asked.append(n)
        if isinstance(self._body, Exception):
            raise self._body
        raw = self._body if isinstance(self._body, bytes) else self._body.encode()
        return raw if n < 0 else raw[:n]


class _Resp:
    def __init__(self, status: int, body: str | bytes | Exception) -> None:
        self.status = status
        self.content = _Stream(body)


async def test_a_410_with_the_code_is_an_envelope_the_peer_holds():
    assert INBOX_REPLAY_STATUS == 410
    assert INBOX_CODE_REPLAY == "REPLAY"
    assert await inbox_holds_envelope(_Resp(410, '{"error":"gone","code":"REPLAY"}'))


@pytest.mark.parametrize(
    "status, body",
    [
        (410, '{"error": "gone"}'),  # skew, or a peer that predates the code
        (410, '{"error": "gone", "code": "replay"}'),
        (410, '{"code": ["REPLAY"]}'),
        (410, '["REPLAY"]'),
        (410, '"REPLAY"'),
        (410, "REPLAY"),
        (410, ""),
        (410, "<html>gone</html>"),
        (410, b"\xff\xfe"),
        (410, ConnectionResetError()),
        # The code on any other status is not that answer.
        (200, '{"code": "REPLAY"}'),
        (400, '{"code": "REPLAY"}'),
        (403, '{"code": "REPLAY"}'),
        (404, '{"code": "REPLAY"}'),
        (500, '{"code": "REPLAY"}'),
    ],
)
async def test_anything_else_is_not(status, body):
    assert not await inbox_holds_envelope(_Resp(status, body))


async def test_only_the_head_of_a_body_is_read():
    """A huge body from whatever answered is never pulled off the socket:
    one bounded read, and a body that does not fit is not that answer."""
    padded = _Resp(410, '{"code": "REPLAY", "pad": "' + "x" * 10_000 + '"}')
    assert not await inbox_holds_envelope(padded)
    assert padded.content.asked == [200]


async def test_a_body_is_not_read_for_another_status():
    resp = _Resp(403, '{"code": "REPLAY"}')
    assert not await inbox_holds_envelope(resp)
    assert resp.content.asked == []
