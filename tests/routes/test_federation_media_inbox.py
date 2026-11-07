"""HTTP tests for binary media bodies on ``POST /federation/inbox/{id}``
(proto v_81).

The body is the ``MEDIA_BODY_MAGIC``, a frame-suite byte and one
``fed-media-v1`` frame — the signed envelope header plus the
AES-256-GCM-sealed chunk as raw bytes. Same URL as a JSON envelope; the
inbox tells them apart from the body alone, so Home Assistant's inbox relay
(which forces ``application/octet-stream`` and drops custom headers)
carries both. Every frame runs the same §24.11 pipeline as a JSON envelope;
these drive it through the real app, directly and through a simulated relay.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import aiohttp
import orjson
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app_keys import (
    federation_repo_key,
    federation_service_key,
    key_manager_key,
    rate_limiter_key,
    space_repo_key,
)
from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
from socialhome.federation import FederationService
from socialhome.federation import media_framing as mf
from socialhome.federation.encoder import FederationEncoder
from socialhome.federation.media_envelope import (
    build_media_envelope,
    encode_media_body,
)
from socialhome.routes.federation import (
    MEDIA_INBOX_RATE_LIMIT,
    MEDIA_INBOX_RATE_WINDOW_S,
)

URL = "/federation/inbox/wh-media"
OCTET = {"Content-Type": "application/octet-stream"}


@pytest.fixture
async def env(client):
    db = client._db
    row = await db.fetchone("SELECT instance_id FROM instance_identity WHERE id='self'")
    fed_repo = client.server.app[federation_repo_key]
    kek = client.server.app[key_manager_key]
    peer_kp = generate_identity_keypair()
    session_key = os.urandom(32)
    wrapped = kek.encrypt(session_key)
    peer = RemoteInstance(
        id=derive_instance_id(peer_kp.public_key),
        display_name="peer",
        remote_identity_pk=peer_kp.public_key.hex(),
        key_self_to_remote=wrapped,
        key_remote_to_self=wrapped,
        remote_inbox_url="https://x/federation/inbox/theirs",
        local_inbox_id="wh-media",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    await fed_repo.save_instance(peer)
    return {
        "own_iid": row["instance_id"],
        "peer_kp": peer_kp,
        "peer": peer,
        "session_key": session_key,
        "fed_repo": fed_repo,
        "fed_svc": client.server.app[federation_service_key],
    }


def _frame(
    env,
    *,
    raw: bytes = b"picture bytes",
    event_type: FederationEventType = FederationEventType.DM_MEDIA_BLOB,
    space_id: str | None = None,
    meta: dict | None = None,
    header_patch: dict | None = None,
    resign: bool = True,
) -> tuple[dict, bytes]:
    """A frame built with the production builder; ``meta`` re-seals the
    metadata with extra / replaced fields, ``header_patch`` changes signed
    header fields (re-signed unless ``resign=False``)."""
    enc = FederationEncoder(env["peer_kp"].private_key)
    header, sealed = build_media_envelope(
        encoder=enc,
        session_key=env["session_key"],
        from_instance=env["peer"].id,
        to_instance=env["own_iid"],
        sig_suite="ed25519",
        event_type=event_type,
        payload={"media_blob_id": "b1", "message_id": "m1", "filename": "a.webp"},
        raw_chunk=raw,
        space_id=space_id,
    )
    if meta is None and header_patch is None:
        return header, sealed
    header.pop("signatures")
    if meta is not None:
        current = orjson.loads(
            enc.decrypt_payload(header["encrypted_payload"], env["session_key"])
        )
        current.update(meta)
        header["encrypted_payload"] = enc.encrypt_payload(
            orjson.dumps(current).decode(), env["session_key"]
        )
    unsigned = dict(header)
    header.update(header_patch or {})
    signed = header if resign else unsigned
    header["signatures"] = enc.sign_envelope_all(orjson.dumps(signed), suite="ed25519")
    return header, sealed


def _dispatch():
    return patch.object(FederationService, "_dispatch_event", new_callable=AsyncMock)


async def _post(client, body: bytes, url: str = URL):
    return await client.post(url, data=body, headers=OCTET)


async def test_a_binary_frame_is_validated_and_dispatched(client, env):
    raw = os.urandom(200_000)
    header, sealed = _frame(env, raw=raw)
    with _dispatch() as dispatch:
        r = await _post(client, encode_media_body(header, sealed))
    assert r.status == 200
    assert (await r.json()) == {"status": "ok"}
    [event] = [c.args[0] for c in dispatch.await_args_list]
    assert event.event_type is FederationEventType.DM_MEDIA_BLOB
    assert event.media_bytes == raw
    assert event.payload["filename"] == "a.webp"


async def test_a_full_512_kib_chunk_fits_the_route(client, env):
    raw = os.urandom(512 * 1024)
    header, sealed = _frame(env, raw=raw)
    body = encode_media_body(header, sealed)
    assert len(body) <= mf.HTTPS_MEDIA_MAX_BODY_BYTES
    with _dispatch() as dispatch:
        r = await _post(client, body)
    assert r.status == 200
    assert dispatch.await_args.args[0].media_bytes == raw


async def test_a_tampered_body_is_refused(client, env):
    header, sealed = _frame(env)
    body = bytearray(encode_media_body(header, sealed))
    body[-1] ^= 0x01
    with _dispatch() as dispatch:
        r = await _post(client, bytes(body))
    assert r.status == 400
    assert (await r.json()) == {"error": "bad_envelope"}
    dispatch.assert_not_awaited()


async def test_a_swapped_chunk_is_refused(client, env):
    header, _ = _frame(env, raw=b"committed")
    other = FederationEncoder(env["peer_kp"].private_key).encrypt_bytes(
        b"swapped", env["session_key"]
    )
    with _dispatch() as dispatch:
        r = await _post(client, encode_media_body(header, other))
    assert r.status == 400
    dispatch.assert_not_awaited()


async def test_a_non_ascii_chunk_sha256_is_a_400_not_a_500(client, env):
    header, sealed = _frame(env, meta={"chunk_sha256": "é" * 43})
    with _dispatch() as dispatch:
        r = await _post(client, encode_media_body(header, sealed))
    assert r.status == 400
    assert (await r.json()) == {"error": "bad_envelope"}
    dispatch.assert_not_awaited()


async def test_a_tampered_header_is_refused(client, env):
    header, sealed = _frame(env, header_patch={"space_id": "sp-x"}, resign=False)
    with _dispatch() as dispatch:
        r = await _post(client, encode_media_body(header, sealed))
    assert r.status == 403
    dispatch.assert_not_awaited()


async def test_a_bad_signature_is_refused(client, env):
    header, sealed = _frame(env)
    header["signatures"] = {"ed25519": "A" * 86}
    with _dispatch() as dispatch:
        r = await _post(client, encode_media_body(header, sealed))
    assert r.status == 403
    dispatch.assert_not_awaited()


async def test_a_replayed_frame_is_refused(client, env):
    header, sealed = _frame(env)
    body = encode_media_body(header, sealed)
    with _dispatch() as dispatch:
        assert (await _post(client, body)).status == 200
        r = await _post(client, body)
    assert r.status == 410
    assert dispatch.await_count == 1


async def test_an_unknown_frame_suite_is_refused(client, env):
    header, sealed = _frame(env, meta={"media_frame_suite": "fed-media-v9"})
    with _dispatch() as dispatch:
        r = await _post(client, encode_media_body(header, sealed))
    assert r.status == 400
    dispatch.assert_not_awaited()


async def test_an_oversize_body_is_refused_before_it_is_read(client, env):
    body = mf.MEDIA_BODY_MAGIC + bytes([1]) + bytes(mf.HTTPS_MEDIA_MAX_BODY_BYTES)
    with (
        _dispatch() as dispatch,
        patch(
            "socialhome.federation.federation_service.FederationService."
            "handle_inbound_media_https",
            new_callable=AsyncMock,
        ) as handler,
    ):
        r = await _post(client, body)
    assert r.status == 413
    handler.assert_not_awaited()
    dispatch.assert_not_awaited()


async def test_a_chunked_media_body_over_the_cap_is_refused(client, env):
    """No ``Content-Length`` (a chunked upload): the cap holds while
    streaming."""

    async def gen():
        yield mf.MEDIA_BODY_MAGIC + bytes([1])
        for _ in range(9):
            yield bytes(64 * 1024)

    with patch(
        "socialhome.federation.federation_service.FederationService."
        "handle_inbound_media_https",
        new_callable=AsyncMock,
    ) as handler:
        r = await client.post(URL, data=gen(), headers=OCTET)
    assert r.status == 413
    handler.assert_not_awaited()


@pytest.mark.parametrize("body", [b"", b"\x00", b"\x00SH", b"{"])
async def test_short_bodies_are_read_as_json_and_refused(client, env, body):
    with _dispatch() as dispatch:
        r = await _post(client, body)
    assert r.status == 400
    dispatch.assert_not_awaited()


async def test_an_unknown_inbox_is_refused(client, env):
    header, sealed = _frame(env)
    with _dispatch() as dispatch:
        r = await _post(
            client,
            encode_media_body(header, sealed),
            url="/federation/inbox/nope",
        )
    assert r.status == 404
    dispatch.assert_not_awaited()


async def test_a_banned_household_gets_the_uniform_answer(client, env):
    await client.server.app[space_repo_key].save(
        Space(
            id="sp1",
            name="sp1",
            owner_instance_id=env["own_iid"],
            owner_username="admin",
            identity_public_key="aabb" * 16,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    await env["fed_repo"].ban_instance_from_space("sp1", env["peer"].id)
    header, sealed = _frame(
        env, event_type=FederationEventType.SPACE_MEDIA_BLOB, space_id="sp1"
    )
    with _dispatch() as dispatch:
        r = await _post(client, encode_media_body(header, sealed))
    assert r.status == 200  # no ban oracle
    dispatch.assert_not_awaited()


async def test_a_stale_timestamp_is_refused(client, env):
    stale = (datetime.now(UTC) - timedelta(seconds=900)).isoformat()
    header, sealed = _frame(env, header_patch={"timestamp": stale})
    with _dispatch() as dispatch:
        r = await _post(client, encode_media_body(header, sealed))
    assert r.status == 410
    dispatch.assert_not_awaited()


async def test_only_media_events_ride_the_media_route(client, env):
    header, sealed = _frame(env, event_type=FederationEventType.DM_MESSAGE)
    with _dispatch() as dispatch:
        r = await _post(client, encode_media_body(header, sealed))
    assert r.status == 400
    dispatch.assert_not_awaited()


async def test_a_bare_frame_without_the_magic_is_refused(client, env):
    """Only the magic makes a body binary — a bare channel frame is read
    as JSON and refused."""
    header, sealed = _frame(env)
    with _dispatch() as dispatch:
        r = await _post(client, mf.encode(orjson.dumps(header), sealed))
    assert r.status == 400
    dispatch.assert_not_awaited()


async def test_the_media_route_has_its_own_rate_limit(client, env):
    limiter = client.server.app[rate_limiter_key]
    bucket = "federation-media:127.0.0.1"
    for _ in range(MEDIA_INBOX_RATE_LIMIT):
        assert limiter.is_allowed(
            bucket, limit=MEDIA_INBOX_RATE_LIMIT, window_s=MEDIA_INBOX_RATE_WINDOW_S
        )
    header, sealed = _frame(env)
    with _dispatch() as dispatch:
        r = await _post(client, encode_media_body(header, sealed))
    assert r.status == 429
    assert (await r.json()) == {"error": "rate_limited"}
    assert 1 <= int(r.headers["Retry-After"]) <= MEDIA_INBOX_RATE_WINDOW_S
    dispatch.assert_not_awaited()


async def test_a_json_envelope_still_lands_on_the_same_url(client, env):
    """The JSON inbox is unchanged — also when it arrives as
    ``application/octet-stream`` (what Home Assistant's relay sends)."""
    body = _json_envelope(env)
    with _dispatch() as dispatch:
        r = await _post(client, body)
    assert r.status == 200
    assert dispatch.await_args.args[0].event_type is (
        FederationEventType.PRESENCE_UPDATED
    )


async def test_an_oversize_json_envelope_is_still_refused(client, env):
    r = await client.post(URL, data=b"{" + bytes(1024 * 1024))
    assert r.status == 413


# ─── Through Home Assistant's inbox relay ─────────────────────────────────
#
# In ha / haos a household advertises Home Assistant's
# ``/api/socialhome/inbox/{inbox_id}`` (the companion integration,
# custom_components/socialhome/federation_inbox.py), which forwards to the
# app's ``POST /federation/inbox/{inbox_id}`` through socialhome-client
# ``forward_inbox_envelope``: the raw body as is, ``Content-Type`` forced
# to ``application/octet-stream``, every custom header dropped, the answer
# (status, body, Content-Type) mirrored back. Simulated here.


@pytest.fixture
async def relay(client):
    async def forward(request: web.Request) -> web.Response:
        body = await request.read()
        target = str(client.make_url(f"/federation/inbox/{request.match_info['iid']}"))
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                target,
                data=body,
                headers={"Content-Type": "application/octet-stream"},
            ) as resp,
        ):
            return web.Response(
                status=resp.status,
                body=await resp.read(),
                content_type=resp.content_type,
            )

    app = web.Application(client_max_size=16 * 1024 * 1024)
    app.router.add_post("/api/socialhome/inbox/{iid}", forward)
    async with TestClient(TestServer(app)) as relay_client:
        yield relay_client


#: Whatever the caller of the relay sends, the relay forwards
#: ``application/octet-stream`` and none of these — the body has to carry
#: everything.
_HEADERS_THE_RELAY_DROPS = {
    "Content-Type": "application/x-anything",
    "X-SocialHome-Anything": "dropped",
}


async def test_a_binary_frame_crosses_the_relay(client, env, relay):
    raw = os.urandom(512 * 1024)
    header, sealed = _frame(env, raw=raw)
    with _dispatch() as dispatch:
        r = await relay.post(
            "/api/socialhome/inbox/wh-media",
            data=encode_media_body(header, sealed),
            headers=_HEADERS_THE_RELAY_DROPS,
        )
    assert r.status == 200
    assert (await r.json()) == {"status": "ok"}
    assert dispatch.await_args.args[0].media_bytes == raw


async def test_a_json_envelope_crosses_the_relay(client, env, relay):
    with _dispatch() as dispatch:
        r = await relay.post(
            "/api/socialhome/inbox/wh-media",
            data=_json_envelope(env),
            headers={"Content-Type": "application/json"},
        )
    assert r.status == 200
    dispatch.assert_awaited_once()


async def test_a_tampered_frame_is_refused_through_the_relay(client, env, relay):
    header, sealed = _frame(env)
    body = bytearray(encode_media_body(header, sealed))
    body[-1] ^= 0x01
    with _dispatch() as dispatch:
        r = await relay.post("/api/socialhome/inbox/wh-media", data=bytes(body))
    assert r.status == 400
    assert (await r.json()) == {"error": "bad_envelope"}
    dispatch.assert_not_awaited()


def _json_envelope(env) -> bytes:
    enc = FederationEncoder(env["peer_kp"].private_key)
    envelope = {
        "msg_id": str(uuid.uuid4()),
        "event_type": FederationEventType.PRESENCE_UPDATED.value,
        "from_instance": env["peer"].id,
        "to_instance": env["own_iid"],
        "timestamp": datetime.now(UTC).isoformat(),
        "encrypted_payload": enc.encrypt_payload(
            orjson.dumps({"user_id": "alice", "state": "home"}).decode(),
            env["session_key"],
        ),
        "space_id": None,
        "proto_version": 1,
        "sig_suite": "ed25519",
    }
    envelope["signatures"] = enc.sign_envelope_all(
        orjson.dumps(envelope), suite="ed25519"
    )
    return orjson.dumps(envelope)
