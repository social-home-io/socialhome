"""The HTTPS leg of binary media (proto v_81): one ``fed-media-v1`` body
POSTed to the peer's own inbox URL as raw bytes — no JSON, no base64, no
new path (Home Assistant's inbox relay forwards it unchanged)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
import orjson
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from socialhome.domain.federation import (
    DeliveryResult,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation import media_framing as mf
from socialhome.federation.media_https_backoff import MediaPostResult
from socialhome.federation.transport import FederationTransport, HttpsInboxTransport


def _instance(url: str) -> RemoteInstance:
    return RemoteInstance(
        id="peer",
        display_name="peer",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url=url,
        local_inbox_id="wh-peer",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )


@pytest.fixture
async def media_server():
    hits: list[tuple[str, str, bytes]] = []
    status: dict = {"code": 200, "headers": {}}

    async def media(request: web.Request) -> web.Response:
        hits.append(
            (request.path, request.content_type, await request.read()),
        )
        return web.json_response(
            {"status": "ok"}, status=status["code"], headers=status["headers"]
        )

    app = web.Application()
    app.router.add_post("/federation/inbox/{inbox_id}", media)
    srv = TestServer(app, host="127.0.0.1")
    await srv.start_server()
    async with aiohttp.ClientSession() as session:

        async def _factory():
            return session

        yield srv, HttpsInboxTransport(client_factory=_factory), hits, status
    await srv.close()


async def _signal(*_a) -> DeliveryResult:  # pragma: no cover — never signalled
    raise AssertionError("no signalling on the media HTTPS leg")


async def test_send_media_posts_raw_bytes_to_the_inbox_url_itself(media_server):
    srv, wt, hits, _ = media_server
    inst = _instance(f"http://127.0.0.1:{srv.port}/federation/inbox/abc")
    body = mf.encode_body(b'{"h":1}', bytes(range(20)))
    res = await wt.send_media(instance=inst, body=body)
    assert res == MediaPostResult(ok=True, status_code=200)
    assert hits == [
        ("/federation/inbox/abc", "application/octet-stream", body),
    ]


async def test_send_media_non_2xx_is_a_failure(media_server):
    srv, wt, _hits, status = media_server
    status["code"] = 404
    inst = _instance(f"http://127.0.0.1:{srv.port}/federation/inbox/abc")
    res = await wt.send_media(instance=inst, body=b"x")
    assert res == MediaPostResult(ok=False, status_code=404)


async def test_send_media_reports_a_429s_retry_after(media_server):
    srv, wt, _hits, status = media_server
    status["code"] = 429
    status["headers"] = {"Retry-After": "17"}
    inst = _instance(f"http://127.0.0.1:{srv.port}/federation/inbox/abc")
    res = await wt.send_media(instance=inst, body=b"x")
    assert res == MediaPostResult(ok=False, status_code=429, retry_after_s=17.0)


@pytest.mark.parametrize("value", ["soon", "-5", ""])
async def test_send_media_ignores_a_retry_after_it_cannot_read(media_server, value):
    srv, wt, _hits, status = media_server
    status["code"] = 429
    status["headers"] = {"Retry-After": value}
    inst = _instance(f"http://127.0.0.1:{srv.port}/federation/inbox/abc")
    res = await wt.send_media(instance=inst, body=b"x")
    assert res == MediaPostResult(ok=False, status_code=429, retry_after_s=None)


async def test_send_media_unreachable_peer_is_a_failure():
    async def _factory():
        class _Raising:
            def post(self, *a, **kw):
                raise aiohttp.ClientConnectionError()

        return _Raising()

    wt = HttpsInboxTransport(client_factory=_factory)
    res = await wt.send_media(instance=_instance("https://p/wh"), body=b"x")
    assert res == MediaPostResult(ok=False, status_code=None)


@pytest.fixture(autouse=True)
def no_handshake():
    """The lazy RTC handshake the media leg kicks — stubbed here (its own
    tests live with the transport); exposed for the tests that check it."""
    with patch.object(
        FederationTransport, "_ensure_handshake", new_callable=AsyncMock
    ) as kick:
        yield kick


async def test_facade_frames_the_header_and_sealed_chunk(media_server):
    srv, wt, hits, _ = media_server
    t = FederationTransport(
        own_instance_id="self",
        https_inbox=wt,
        signaling_send=_signal,
    )
    inst = _instance(f"http://127.0.0.1:{srv.port}/federation/inbox/abc")
    header = {"msg_id": "m1", "signatures": {"ed25519": "s"}}
    res = await t.send_media_https(
        instance=inst, header_dict=header, payload_bytes=b"sealed"
    )
    assert res.ok
    [(_path, _ctype, body)] = hits
    suite, frame = mf.decode_body(body)
    assert suite == mf.MEDIA_FRAME_SUITE_V1
    assert frame.header == orjson.dumps(header)  # signed bytes verbatim
    assert frame.payload == b"sealed"


async def test_facade_reports_a_refused_frame(media_server):
    srv, wt, _hits, status = media_server
    status["code"] = 429
    status["headers"] = {"Retry-After": "3"}
    t = FederationTransport(
        own_instance_id="self",
        https_inbox=wt,
        signaling_send=_signal,
    )
    inst = _instance(f"http://127.0.0.1:{srv.port}/federation/inbox/abc")
    res = await t.send_media_https(
        instance=inst, header_dict={"msg_id": "m"}, payload_bytes=b"x"
    )
    assert res == MediaPostResult(ok=False, status_code=429, retry_after_s=3.0)


async def test_facade_kicks_the_handshake_for_a_peer_without_a_connection(
    media_server, no_handshake
):
    """Media-only traffic still brings the channel up, as ``send`` does."""
    srv, wt, _hits, _ = media_server
    t = FederationTransport(
        own_instance_id="self", https_inbox=wt, signaling_send=_signal
    )
    inst = _instance(f"http://127.0.0.1:{srv.port}/federation/inbox/abc")
    await t.send_media_https(instance=inst, header_dict={"m": 1}, payload_bytes=b"x")
    no_handshake.assert_awaited_once_with(inst)


async def test_facade_respects_the_rtc_backoff(media_server, no_handshake):
    srv, wt, _hits, _ = media_server
    t = FederationTransport(
        own_instance_id="self", https_inbox=wt, signaling_send=_signal
    )
    inst = _instance(f"http://127.0.0.1:{srv.port}/federation/inbox/abc")
    with patch.object(FederationTransport, "_rtc_suppressed", return_value=True):
        await t.send_media_https(
            instance=inst, header_dict={"m": 1}, payload_bytes=b"x"
        )
    no_handshake.assert_not_awaited()


async def test_facade_never_posts_a_body_over_the_receivers_cap(media_server):
    srv, wt, hits, _ = media_server
    t = FederationTransport(
        own_instance_id="self", https_inbox=wt, signaling_send=_signal
    )
    inst = _instance(f"http://127.0.0.1:{srv.port}/federation/inbox/abc")
    res = await t.send_media_https(
        instance=inst,
        header_dict={"m": 1},
        payload_bytes=bytes(mf.HTTPS_MEDIA_MAX_BODY_BYTES),
    )
    # Reported as the 413 the receiver would answer, so the sender's
    # backoff turns the leg off for this peer instead of retrying it.
    assert res == MediaPostResult(ok=False, status_code=413)
    assert hits == []
