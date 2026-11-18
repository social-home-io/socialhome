"""The sender's reading of the inbox's "already held" answer."""

from __future__ import annotations

import pytest

from socialhome.federation.inbox_reply import (
    INBOX_CODE_REPLAY,
    INBOX_REPLAY_STATUS,
    inbox_holds_envelope,
)


class _Resp:
    def __init__(self, status: int, body: str | Exception) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


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
        (410, UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")),
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


async def test_only_the_head_of_a_body_is_parsed():
    """A huge body from whatever answered is never parsed whole."""
    padded = '{"code": "REPLAY", "pad": "' + "x" * 10_000 + '"}'
    assert not await inbox_holds_envelope(_Resp(410, padded))
