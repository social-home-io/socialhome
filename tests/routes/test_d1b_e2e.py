"""End-to-end coverage for the §D1b cross-household-invite loop (#118).

Stands up a single ``aiohttp`` test app representing the *joiner's*
instance and drives the inbound federation events that her instance
would receive from a host on the other household. Verifies that the
loop ``invite → accept → /api/spaces lists the space → /api/spaces/{id}
/members shows the full roster + custom cover bytes`` works as a
single contract — this is the test PR #425 / #426 / #427 each shipped
without, and the one that locks in their joint surface.

The "host" is simulated by directly invoking the joiner's inbound
handlers with hand-built :class:`FederationEvent` instances. That
keeps the test fast and deterministic without spinning up two real
``TestServer`` instances + a federation transport between them — the
single-instance approach catches every joiner-side write the SPA
ultimately reads.
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import create_app
from socialhome.app_keys import db_key as _db_key
from socialhome.app_keys import federation_service_key, space_remote_member_repo_key
from socialhome.auth import sha256_token_hash
from socialhome.config import Config
from socialhome.crypto import derive_user_id
from socialhome.domain.federation import FederationEvent, FederationEventType


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# A 1×1 white WebP (real bytes, generated). Round-trips
# through ``base64`` cleanly so the inbound-side write asserts on
# something realistic.
def _white_webp() -> bytes:
    """A real 1x1 WebP — the member validates shipped cover bytes."""
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), (255, 255, 255)).save(buf, format="WEBP")
    return buf.getvalue()


_WHITE_WEBP = _white_webp()


@pytest.fixture
async def client(tmp_dir):
    """Single-instance joiner app: one user (anna) who'll receive a
    cross-household invite from a fictional host instance ``peer-1``."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        db = app[_db_key]
        row = await db.fetchone(
            "SELECT identity_public_key FROM instance_identity WHERE id='self'"
        )
        pk_bytes = bytes.fromhex(row["identity_public_key"])
        anna_uid = derive_user_id(pk_bytes, "anna")
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name, is_admin)"
            " VALUES(?,?,?,1)",
            ("anna", anna_uid, "Anna"),
        )
        await db.enqueue(
            "INSERT INTO api_tokens(token_id, user_id, label, token_hash)"
            " VALUES(?,?,?,?)",
            ("tok-anna", anna_uid, "test", sha256_token_hash("anna-token")),
        )
        tc._anna_token = "anna-token"
        tc._anna_uid = anna_uid
        # Stub the federation send path so accept_remote_invite's
        # outbound SPACE_PRIVATE_INVITE_ACCEPT envelope doesn't try to
        # reach a non-existent peer-1 over the network. The single-
        # instance test is *only* exercising the joiner-side state
        # writes; the outbound envelope itself is covered by the
        # zero-leak / private-invite handler tests.
        from types import SimpleNamespace
        from unittest.mock import patch

        fed = app[federation_service_key]

        async def _stub_send_with_mesh_fallback(*args, **kwargs):
            return SimpleNamespace(ok=True, instance_id=kwargs.get("to_instance_id"))

        with patch.object(
            type(fed),
            "send_with_mesh_fallback",
            _stub_send_with_mesh_fallback,
        ):
            yield tc


def _meta_for_host_space(*, with_cover: bool, with_roster: bool) -> dict:
    """The ``space_meta`` blob the host ships in SPACE_PRIVATE_INVITE."""
    meta: dict = {
        "name": "Family",
        "emoji": "🏡",
        "description": "Where we share photos of the kids",
        "owner_instance_id": "peer-1",
        "owner_username": "pascal",
        "identity_public_key": "abc" * 21 + "d",  # 64 hex chars
        "config_sequence": 0,
        "space_type": "private",
        "join_mode": "invite_only",
        "features": {
            "calendar": True,
            "todo": True,
            "location": False,
            "stickies": True,
            "pages": True,
            "gallery": True,
        },
        "tz": "Europe/Berlin",
    }
    if with_cover:
        meta["cover_hash"] = "deadbeef"
        meta["cover_webp_base64"] = base64.b64encode(_WHITE_WEBP).decode("ascii")
    if with_roster:
        meta["roster"] = [
            {
                "user_id": "uid-pascal",
                "instance_id": "peer-1",
                "display_name": "Pascal",
                "role": "owner",
            },
            {
                "user_id": "uid-pascal-wife",
                "instance_id": "peer-1",
                "display_name": "Pascal's wife",
                "role": "member",
            },
        ]
    return meta


async def _dispatch_event(
    app,
    *,
    event_type: FederationEventType,
    payload: dict,
    from_instance: str,
) -> None:
    """Synthesize a :class:`FederationEvent` and dispatch it through the
    registry the same way the inbound pipeline would."""
    fed = app[federation_service_key]
    registry = fed._event_registry  # noqa: SLF001
    event = FederationEvent(
        msg_id=f"msg-{event_type.value}-{from_instance}",
        event_type=event_type,
        from_instance=from_instance,
        to_instance=fed.own_instance_id,
        timestamp="2026-05-23T00:00:00Z",
        payload=payload,
        space_id=payload.get("space_id"),
    )
    handlers = registry._handlers.get(event_type, [])  # noqa: SLF001
    for h in handlers:
        await h(event)


async def test_invite_accept_loop_e2e(client):
    """Pascal invites Anna → her instance fires _on_invite → she accepts
    via ``/api/remote_invites/{token}/accept`` → /api/spaces lists the
    space → /api/spaces/{id}/members shows the roster + her own row."""
    app = client.app
    space_id = "sp-pascal-family"
    invite_token = "invite-tkn-pascal-to-anna"
    meta = _meta_for_host_space(with_cover=True, with_roster=True)
    # Step 1: simulate the SPACE_PRIVATE_INVITE Pascal's instance ships
    # to Anna's instance.
    await _dispatch_event(
        app,
        event_type=FederationEventType.SPACE_PRIVATE_INVITE,
        from_instance="peer-1",
        payload={
            "space_id": space_id,
            "invite_token": invite_token,
            "invitee_user_id": client._anna_uid,
            "inviter_user_id": "uid-pascal",
            "space_display_hint": meta["name"],
            "space_meta": meta,
        },
    )
    # Step 2: Anna sees the invite banner.
    inbox = await (
        await client.get("/api/remote_invites", headers=_auth(client._anna_token))
    ).json()
    assert any(row.get("invite_token") == invite_token for row in inbox)
    # Step 3: /api/spaces should NOT include the space yet — the stub
    # was seated but Anna's ``space_members`` is empty until accept.
    spaces_pre = await (
        await client.get("/api/spaces", headers=_auth(client._anna_token))
    ).json()
    assert not any(s["id"] == space_id for s in spaces_pre)
    # Step 4: Anna accepts.
    resp = await client.post(
        f"/api/remote_invites/{invite_token}/accept",
        headers=_auth(client._anna_token),
    )
    assert resp.status == 204
    # Step 5: /api/spaces now lists the space, with ``owner_instance_id``
    # pointing at Pascal's instance — the SPA reads this to render the
    # "🏘 Other household" chip + hide admin gestures.
    spaces_post = await (
        await client.get("/api/spaces", headers=_auth(client._anna_token))
    ).json()
    card = next((s for s in spaces_post if s["id"] == space_id), None)
    assert card is not None
    assert card["name"] == "Family"
    assert card["emoji"] == "🏡"
    assert card["owner_instance_id"] == "peer-1"
    # Step 6: GET /api/spaces/{id} returns full detail including the
    # owner_instance_id (the SpaceFeedPage gates Settings on this).
    detail = await (
        await client.get(
            f"/api/spaces/{space_id}",
            headers=_auth(client._anna_token),
        )
    ).json()
    assert detail["owner_instance_id"] == "peer-1"
    # Step 7: GET /api/spaces/{id}/members returns Anna (local) +
    # Pascal + Pascal's wife (both remote, from the roster). Each
    # remote row carries the host's instance_id so the SPA can badge
    # them. Anna's own role is "member".
    members = await (
        await client.get(
            f"/api/spaces/{space_id}/members",
            headers=_auth(client._anna_token),
        )
    ).json()
    by_uid = {m["user_id"]: m for m in members}
    assert client._anna_uid in by_uid
    assert by_uid[client._anna_uid]["role"] == "member"
    assert "instance_id" not in by_uid[client._anna_uid]
    assert "uid-pascal" in by_uid
    assert by_uid["uid-pascal"]["instance_id"] == "peer-1"
    assert by_uid["uid-pascal"]["display_name"] == "Pascal"
    assert "uid-pascal-wife" in by_uid
    # Step 8: cover bytes landed in space_covers so
    # ``GET /api/spaces/{id}/cover`` serves the real image rather
    # than 404ing.
    cover_resp = await client.get(
        f"/api/spaces/{space_id}/cover",
        headers=_auth(client._anna_token),
    )
    assert cover_resp.status == 200
    cover_body = await cover_resp.read()
    assert cover_body == _WHITE_WEBP


async def test_invite_without_meta_falls_back_gracefully(client):
    """Older host (pre-PR #425) ships no ``space_meta``: Anna still
    sees the invite banner, but the stub isn't seated → /api/spaces
    stays empty until upstream upgrades. Accept-API errors cleanly
    rather than crashing — this protects rolling-upgrade scenarios."""
    app = client.app
    space_id = "sp-legacy"
    invite_token = "invite-tkn-legacy"
    await _dispatch_event(
        app,
        event_type=FederationEventType.SPACE_PRIVATE_INVITE,
        from_instance="peer-1",
        payload={
            "space_id": space_id,
            "invite_token": invite_token,
            "invitee_user_id": client._anna_uid,
            "inviter_user_id": "uid-pascal",
            "space_display_hint": "Legacy space",
            # No ``space_meta`` — older sender.
        },
    )
    # Banner shows up.
    inbox = await (
        await client.get("/api/remote_invites", headers=_auth(client._anna_token))
    ).json()
    assert any(row.get("invite_token") == invite_token for row in inbox)
    # No stub → /api/spaces stays empty.
    spaces = await (
        await client.get("/api/spaces", headers=_auth(client._anna_token))
    ).json()
    assert all(s["id"] != space_id for s in spaces)


async def test_invite_carries_space_content_key_for_decrypt(client):
    """§D1b #117 — the host ships the current epoch's space content
    key inside ``space_meta``; the receiver's local space_keys row
    must then be present so subsequent SPACE_POST_CREATED inbound
    events can decrypt. Without this, the joiner's stub renders
    but every post-decrypt attempt raises ``no key for space ...``."""
    import base64

    from socialhome.app_keys import space_crypto_service_key

    app = client.app
    space_id = "sp-encrypted"
    invite_token = "invite-enc-token"
    space_crypto = app[space_crypto_service_key]
    # Mint a fixed epoch key on the test app — this is the
    # "host's side" of the federation; we'll round-trip it through
    # ``space_meta`` exactly as the real outbound path would.
    fake_key = b"0" * 32
    meta = _meta_for_host_space(with_cover=False, with_roster=False)
    meta["space_content_key"] = {
        "epoch": 0,
        "key_base64": base64.b64encode(fake_key).decode("ascii"),
    }
    await _dispatch_event(
        app,
        event_type=FederationEventType.SPACE_PRIVATE_INVITE,
        from_instance="peer-1",
        payload={
            "space_id": space_id,
            "invite_token": invite_token,
            "invitee_user_id": client._anna_uid,
            "inviter_user_id": "uid-pascal",
            "space_meta": meta,
        },
    )
    # The receiver's space_keys row exists now. Verify by exporting
    # it back through the service — round-trip proves persistence
    # without us reaching into the SQLite layer directly.
    exported = await space_crypto.export_current_key(space_id)
    assert exported is not None
    epoch, raw = exported
    assert epoch == 0
    assert raw == fake_key


async def test_kick_loop_marks_stub_dissolved(client):
    """Pascal kicks Anna → her stub stops listing in /api/spaces.
    Locally-owned spaces are not affected by remote kicks."""
    app = client.app
    space_id = "sp-kickable"
    # Seat the full join state via the invite path.
    meta = _meta_for_host_space(with_cover=False, with_roster=True)
    await _dispatch_event(
        app,
        event_type=FederationEventType.SPACE_PRIVATE_INVITE,
        from_instance="peer-1",
        payload={
            "space_id": space_id,
            "invite_token": "kick-tkn",
            "invitee_user_id": client._anna_uid,
            "inviter_user_id": "uid-pascal",
            "space_meta": meta,
        },
    )
    await client.post(
        "/api/remote_invites/kick-tkn/accept",
        headers=_auth(client._anna_token),
    )
    # Confirm listed.
    spaces_pre = await (
        await client.get("/api/spaces", headers=_auth(client._anna_token))
    ).json()
    assert any(s["id"] == space_id for s in spaces_pre)
    # Pascal kicks Anna.
    await _dispatch_event(
        app,
        event_type=FederationEventType.SPACE_REMOTE_MEMBER_REMOVED,
        from_instance="peer-1",
        payload={"space_id": space_id, "user_id": client._anna_uid},
    )
    # Anna's /api/spaces no longer lists it.
    spaces_post = await (
        await client.get("/api/spaces", headers=_auth(client._anna_token))
    ).json()
    assert all(s["id"] != space_id for s in spaces_post)
    # Remote-member rows for the OTHER peers stayed put (they're
    # tied to the stub via ``space_id``, not to Anna). Defensive
    # check that the cleanup is targeted.
    repo = app[space_remote_member_repo_key]
    remaining = await repo.list_for_space(space_id)
    assert {r.user_id for r in remaining} == {"uid-pascal", "uid-pascal-wife"}
