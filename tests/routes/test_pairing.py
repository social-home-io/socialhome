"""Route tests for /api/pairing/* and /api/connections (§11, §23.71)."""

from __future__ import annotations


from datetime import datetime, timezone

from socialhome.app_keys import (
    db_key as _db_key,
    dm_routing_service_key,
    federation_repo_key,
    federation_service_key,
    federation_transport_key,
    outbox_repo_key,
    peer_home_sharing_service_key,
)
from socialhome.config import Config
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
)
from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.repositories.dm_routing_repo import SqliteDmRoutingRepo
from socialhome.services.dm_routing_service import DmRoutingService

from .conftest import _auth


async def _seed_relay(svc: DmRoutingService, *, target: str, via: str, ts: str) -> None:
    """Seed a relay path row into the SQLite repo for test fixtures.

    Calls the test-only :meth:`SqliteDmRoutingRepo.insert_relay_path_for_test`
    directly on the concrete repo — keeping production service code free of
    test-seeding logic.  The cast lives here (test boundary) where it belongs.
    """
    repo = svc._repo
    assert isinstance(repo, SqliteDmRoutingRepo), (
        "_seed_relay requires SqliteDmRoutingRepo"
    )
    await repo.insert_relay_path_for_test(
        conversation_id=f"test-relay-{target}",
        sender_user_id="test-sender",
        target_instance=target,
        via=via,
        ts=ts,
    )


def _fake_instance(iid: str = "peer-1") -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=iid,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://peer/wh",
        local_inbox_id=f"wh-{iid}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )


async def test_initiate_pairing_returns_qr_payload(client):
    # Body is empty — server sources the inbox base URL from the
    # platform adapter (seeded via [standalone].external_url in
    # tests/routes/conftest.py).
    r = await client.post(
        "/api/pairing/initiate",
        json={},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    data = await r.json()
    assert "token" in data and "identity_pk" in data and "dh_pk" in data
    # Advertised URL = seeded base + "/" + generated own_local_inbox_id.
    assert data["inbox_url"].startswith(
        "https://test.example/federation/inbox/",
    )


async def test_initiate_pairing_not_configured_when_base_missing(
    tmp_dir, aiohttp_client
):
    """422 NOT_CONFIGURED when [standalone].external_url is unset."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
    )
    from socialhome.app import create_app
    from socialhome.app_keys import db_key as _db_key
    from socialhome.auth import sha256_token_hash
    from socialhome.crypto import derive_user_id

    app = create_app(cfg)
    tc = await aiohttp_client(app)
    db = app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    pk_bytes = bytes.fromhex(row["identity_public_key"])
    uid = derive_user_id(pk_bytes, "admin")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,1)",
        ("admin", uid, "Admin"),
    )
    raw = "tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t1", uid, "t", sha256_token_hash(raw)),
    )
    r = await tc.post(
        "/api/pairing/initiate",
        json={},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status == 422
    body = await r.json()
    assert body["error"]["code"] == "NOT_CONFIGURED"


async def test_initiate_pairing_bad_json_still_ok(client):
    """Unparseable body used to be 400 — now the body is ignored entirely,
    so the route proceeds on the adapter-provided base and returns 201.
    """
    r = await client.post(
        "/api/pairing/initiate",
        data="nope",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 201


async def test_accept_pairing_rejects_malformed(client):
    r = await client.post(
        "/api/pairing/accept",
        json={"only": "this"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_confirm_pairing_missing_fields(client):
    r = await client.post(
        "/api/pairing/confirm",
        json={"token": "t"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_confirm_pairing_unknown_token(client):
    r = await client.post(
        "/api/pairing/confirm",
        json={"token": "nope", "verification_code": "000000"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_list_connections_empty(client):
    r = await client.get(
        "/api/pairing/connections",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert await r.json() == []


async def test_list_connections_returns_instances(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-1"))
    r = await client.get(
        "/api/pairing/connections",
        headers=_auth(client._tok),
    )
    data = await r.json()
    assert len(data) == 1
    assert data[0]["instance_id"] == "peer-1"
    assert data[0]["status"] == "confirmed"


async def test_connections_alias_matches_pairing_list(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-2"))
    r = await client.get("/api/connections", headers=_auth(client._tok))
    assert r.status == 200
    assert (await r.json())[0]["instance_id"] == "peer-2"


async def test_unpair_missing_instance_returns_404(client):
    r = await client.delete(
        "/api/pairing/connections/nope",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_unpair_removes_instance(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-3"))
    r = await client.delete(
        "/api/pairing/connections/peer-3",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    # Gone from listing.
    r = await client.get(
        "/api/pairing/connections",
        headers=_auth(client._tok),
    )
    assert (await r.json()) == []


async def test_connections_endpoint_does_not_leak_session_keys(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-4"))
    r = await client.get("/api/connections", headers=_auth(client._tok))
    data = await r.json()
    row = data[0]
    assert "key_self_to_remote" not in row
    assert "key_remote_to_self" not in row
    assert "remote_identity_pk" not in row


# ─── Pairing introduce (§11.9) ─────────────────────────────────────────────


async def test_introduce_rejects_missing_fields(client):
    r = await client.post(
        "/api/pairing/introduce",
        json={"target_instance_id": "iid"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_introduce_rejects_self_referential(client):
    r = await client.post(
        "/api/pairing/introduce",
        json={"target_instance_id": "x", "via_instance_id": "x"},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_introduce_unknown_relay_peer_404(client):
    r = await client.post(
        "/api/pairing/introduce",
        json={"target_instance_id": "target", "via_instance_id": "nobody"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_introduce_bad_json_400(client):
    r = await client.post(
        "/api/pairing/introduce",
        data="not-json",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


# ─── Pairing relay requests (§11.9 approve/decline) ────────────────────────


async def test_relay_requests_list_empty(client):
    r = await client.get(
        "/api/pairing/relay-requests",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert await r.json() == []


async def test_relay_approve_unknown_returns_404(client):
    r = await client.post(
        "/api/pairing/relay-requests/nope/approve",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_relay_decline_unknown_returns_404(client):
    r = await client.post(
        "/api/pairing/relay-requests/nope/decline",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_relay_list_approve_decline_full_flow(client):
    """Seed the queue via the bus, then approve / decline via HTTP."""
    from socialhome.app_keys import pairing_relay_queue_key
    from socialhome.domain.events import PairingIntroRelayReceived
    from socialhome.infrastructure.event_bus import EventBus

    queue = client.app[pairing_relay_queue_key]
    # Inject two pending requests directly via the bus the queue subscribed to.
    bus: EventBus = queue._bus
    await bus.publish(
        PairingIntroRelayReceived(
            from_instance="peer-a",
            target_instance_id="peer-b",
            message="intro",
        )
    )
    await bus.publish(
        PairingIntroRelayReceived(
            from_instance="peer-c",
            target_instance_id="peer-d",
        )
    )

    r = await client.get(
        "/api/pairing/relay-requests",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    items = await r.json()
    assert len(items) == 2
    req_id = items[0]["id"]

    # Decline the first
    r = await client.post(
        f"/api/pairing/relay-requests/{req_id}/decline",
        headers=_auth(client._tok),
    )
    assert r.status == 204

    r = await client.get(
        "/api/pairing/relay-requests",
        headers=_auth(client._tok),
    )
    remaining = await r.json()
    assert len(remaining) == 1


# ── /api/pairing/connections/{id}/visible-users ────────────────────────


async def _seed_extra_user(client, username: str) -> str:
    """Create a second local user, return their user_id."""
    from socialhome.app_keys import db_key as _db_key
    from socialhome.crypto import derive_user_id

    db = client.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'",
    )
    pk_bytes = bytes.fromhex(row["identity_public_key"])
    uid = derive_user_id(pk_bytes, username)
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        (username, uid, username.title()),
    )
    return uid


async def test_visible_users_get_returns_default_visible(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-vis-1"))
    extra_uid = await _seed_extra_user(client, "lily")

    r = await client.get(
        "/api/pairing/connections/peer-vis-1/visible-users",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    rows = {u["user_id"]: u for u in body["users"]}
    # Both admin and the freshly-seeded ``lily`` default to visible.
    assert client._uid in rows and rows[client._uid]["visible"] is True
    assert extra_uid in rows and rows[extra_uid]["visible"] is True


async def test_visible_users_patch_hides_user_and_sends_user_removed(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-vis-2"))
    extra_uid = await _seed_extra_user(client, "kai")

    captured: list[dict] = []

    class _Recorder:
        async def send_event(self, *, to_instance_id, event_type, payload):
            captured.append(
                {"to": to_instance_id, "type": event_type, "payload": payload},
            )

    from socialhome.app_keys import federation_service_key

    client.app[federation_service_key] = _Recorder()

    r = await client.patch(
        "/api/pairing/connections/peer-vis-2/visible-users",
        json={"updates": [{"user_id": extra_uid, "visible": False}]},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    rows = {u["user_id"]: u for u in body["users"]}
    assert rows[extra_uid]["visible"] is False

    from socialhome.domain.federation import FederationEventType

    assert len(captured) == 1
    assert captured[0]["to"] == "peer-vis-2"
    assert captured[0]["type"] is FederationEventType.USER_REMOVED
    assert captured[0]["payload"] == {"user_id": extra_uid}


async def test_visible_users_patch_unhide_sends_user_updated(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-vis-3"))
    extra_uid = await _seed_extra_user(client, "max")

    from socialhome.app_keys import (
        federation_service_key,
        peer_user_visibility_repo_key,
    )

    # Pre-hide the user via the repo so the PATCH path tests the
    # hidden→visible transition specifically.
    vis_repo = client.app[peer_user_visibility_repo_key]
    await vis_repo.set_visibility(
        instance_id="peer-vis-3",
        user_id=extra_uid,
        visible=False,
        set_by=None,
    )

    captured: list[dict] = []

    class _Recorder:
        async def send_event(self, *, to_instance_id, event_type, payload):
            captured.append({"type": event_type, "payload": payload})

    client.app[federation_service_key] = _Recorder()

    r = await client.patch(
        "/api/pairing/connections/peer-vis-3/visible-users",
        json={"updates": [{"user_id": extra_uid, "visible": True}]},
        headers=_auth(client._tok),
    )
    assert r.status == 200

    from socialhome.domain.federation import FederationEventType

    assert len(captured) == 1
    assert captured[0]["type"] is FederationEventType.USER_UPDATED
    assert captured[0]["payload"]["user_id"] == extra_uid


async def test_visible_users_patch_no_op_when_already_in_target_state(client):
    """Visible-→visible flip is a no-op (already-visible default)."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-vis-4"))
    extra_uid = await _seed_extra_user(client, "noop")

    captured: list[dict] = []

    class _Recorder:
        async def send_event(self, *, to_instance_id, event_type, payload):
            captured.append({})

    from socialhome.app_keys import federation_service_key

    client.app[federation_service_key] = _Recorder()

    r = await client.patch(
        "/api/pairing/connections/peer-vis-4/visible-users",
        json={"updates": [{"user_id": extra_uid, "visible": True}]},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    # Already-visible to already-visible — no envelope sent.
    assert captured == []


async def test_visible_users_patch_rejects_unknown_user(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-vis-5"))

    r = await client.patch(
        "/api/pairing/connections/peer-vis-5/visible-users",
        json={"updates": [{"user_id": "not-a-real-user", "visible": False}]},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_visible_users_404_for_unknown_peer(client):
    r = await client.get(
        "/api/pairing/connections/no-such-peer/visible-users",
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_visible_users_requires_admin(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-vis-6"))

    from socialhome.app_keys import db_key as _db_key

    db = client.app[_db_key]
    await db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    r = await client.get(
        "/api/pairing/connections/peer-vis-6/visible-users",
        headers=_auth(client._tok),
    )
    assert r.status == 403


async def test_relay_requests_require_admin(client):
    """Non-admin user gets 403."""
    from socialhome.app_keys import db_key as _db_key
    from socialhome.auth import sha256_token_hash
    from socialhome.crypto import derive_user_id

    db = client.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'",
    )
    pk_bytes = bytes.fromhex(row["identity_public_key"])
    uid = derive_user_id(pk_bytes, "regular")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("regular", uid, "Regular"),
    )
    raw = "regular-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-reg", uid, "t", sha256_token_hash(raw)),
    )

    r = await client.get(
        "/api/pairing/relay-requests",
        headers=_auth(raw),
    )
    assert r.status == 403


# ─── Local alias on a paired peer (PR A) ──────────────────────────────────


async def test_alias_patch_sets_alias_and_returns_effective_name(client):
    fed_repo = client.app[federation_repo_key]
    inst = _fake_instance("peer-alias-1")
    # Simulate the cryptic federated display_name the user actually
    # sees today (truncated instance_id).
    await fed_repo.save_instance(inst)

    r = await client.patch(
        "/api/pairing/connections/peer-alias-1/alias",
        json={"alias": "Brother's house"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["instance_id"] == "peer-alias-1"
    assert body["local_alias"] == "Brother's house"
    assert body["effective_display_name"] == "Brother's house"
    # Federated name unchanged — we only renamed locally.
    assert body["display_name"] == "peer-alias-1"

    # GET /api/pairing/connections now returns the effective name in
    # ``display_name`` (the SPA-facing field).
    listing = await (
        await client.get(
            "/api/pairing/connections",
            headers=_auth(client._tok),
        )
    ).json()
    row = next(r for r in listing if r["instance_id"] == "peer-alias-1")
    assert row["display_name"] == "Brother's house"
    assert row["federated_display_name"] == "peer-alias-1"
    assert row["local_alias"] == "Brother's house"


async def test_alias_patch_clear_with_null(client):
    """``{"alias": null}`` clears the alias; effective name falls back."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-alias-2"))
    await fed_repo.update_alias("peer-alias-2", "Temporary")

    r = await client.patch(
        "/api/pairing/connections/peer-alias-2/alias",
        json={"alias": None},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["local_alias"] is None
    assert body["effective_display_name"] == "peer-alias-2"


async def test_alias_patch_whitespace_clears(client):
    """Whitespace-only alias is treated as a clear — keeps the picker
    from showing a blank effective name."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-alias-3"))
    await fed_repo.update_alias("peer-alias-3", "Some name")

    r = await client.patch(
        "/api/pairing/connections/peer-alias-3/alias",
        json={"alias": "   "},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["local_alias"] is None


async def test_alias_patch_rejects_too_long(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-alias-4"))
    r = await client.patch(
        "/api/pairing/connections/peer-alias-4/alias",
        json={"alias": "x" * 81},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_alias_patch_rejects_non_string(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-alias-5"))
    r = await client.patch(
        "/api/pairing/connections/peer-alias-5/alias",
        json={"alias": 42},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_alias_patch_unknown_peer_returns_404(client):
    r = await client.patch(
        "/api/pairing/connections/no-such-peer/alias",
        json={"alias": "Anything"},
        headers=_auth(client._tok),
    )
    assert r.status == 404


async def test_alias_patch_requires_admin(client):
    """Non-admin token is rejected — local rename is an admin
    concern, same gate the other connection-edit views use."""
    from socialhome.app_keys import db_key as _db_key
    from socialhome.auth import sha256_token_hash
    from socialhome.crypto import derive_user_id

    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-alias-6"))

    db = client.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'",
    )
    pk_bytes = bytes.fromhex(row["identity_public_key"])
    uid = derive_user_id(pk_bytes, "member")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("member", uid, "Member"),
    )
    raw = "member-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-mem", uid, "t", sha256_token_hash(raw)),
    )

    r = await client.patch(
        "/api/pairing/connections/peer-alias-6/alias",
        json={"alias": "nope"},
        headers=_auth(raw),
    )
    assert r.status == 403


async def test_connections_response_carries_transport_rtc(client):
    """A confirmed peer whose DataChannel is open reports transport='rtc'."""
    kp = generate_identity_keypair()
    peer = RemoteInstance(
        id=derive_instance_id(kp.public_key),
        display_name="peer-rtc",
        remote_identity_pk=kp.public_key.hex(),
        key_self_to_remote="k",
        key_remote_to_self="k",
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-rtc",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(peer)

    class _AlwaysOpenTransport:
        def is_ready(self, instance_id):
            return True

    client.app[federation_transport_key] = _AlwaysOpenTransport()

    r = await client.get(
        "/api/connections",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == peer.id)
    assert row["transport"] == "rtc"


async def test_connections_response_transport_https_when_channel_down(client):
    """Same peer, transport service reports not-ready → transport='https'."""
    kp = generate_identity_keypair()
    peer = RemoteInstance(
        id=derive_instance_id(kp.public_key),
        display_name="peer-https",
        remote_identity_pk=kp.public_key.hex(),
        key_self_to_remote="k",
        key_remote_to_self="k",
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-https",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(peer)

    class _NeverOpenTransport:
        def is_ready(self, instance_id):
            return False

    client.app[federation_transport_key] = _NeverOpenTransport()

    r = await client.get(
        "/api/connections",
        headers=_auth(client._tok),
    )
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == peer.id)
    assert row["transport"] == "https"


async def test_connections_response_transport_null_when_unreachable(client):
    """An unreachable confirmed peer has transport=null — we don't claim
    a transport for a peer we can't reach."""
    kp = generate_identity_keypair()
    peer = RemoteInstance(
        id=derive_instance_id(kp.public_key),
        display_name="peer-unreach",
        remote_identity_pk=kp.public_key.hex(),
        key_self_to_remote="k",
        key_remote_to_self="k",
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-unreach",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(peer)
    await fed_repo.mark_unreachable(peer.id)

    class _NeverOpenTransport:
        def is_ready(self, instance_id):
            return False

    client.app[federation_transport_key] = _NeverOpenTransport()

    r = await client.get(
        "/api/connections",
        headers=_auth(client._tok),
    )
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == peer.id)
    assert row["transport"] is None


async def test_connections_response_transport_https_when_no_rtc_wired(client):
    """No federation_transport_key in app → confirmed peer still gets
    transport='https'. Models a deployment without RTC wiring (e.g. a
    stripped harness) where federation has to fall back to HTTPS-only.
    """
    kp = generate_identity_keypair()
    peer = RemoteInstance(
        id=derive_instance_id(kp.public_key),
        display_name="peer-no-rtc",
        remote_identity_pk=kp.public_key.hex(),
        key_self_to_remote="k",
        key_remote_to_self="k",
        remote_inbox_url="https://x/wh",
        local_inbox_id="wh-no-rtc",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(peer)

    # Deliberately drop the transport key — simulate a no-RTC build.
    client.app[federation_transport_key] = None

    r = await client.get(
        "/api/connections",
        headers=_auth(client._tok),
    )
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == peer.id)
    assert row["transport"] == "https"


# ─── Transport detail (Task 6) ───────────────────────────────────────────────


async def test_transport_detail_returns_recent_relay(client):
    """For a peer with a recent DM relay, the endpoint returns
    {last_relay: {via, ts}}."""
    svc = client.app[dm_routing_service_key]
    now = datetime.now(timezone.utc).isoformat()
    await _seed_relay(svc, target="peer-target", via="peer-relay", ts=now)

    r = await client.get(
        "/api/pairing/connections/peer-target/transport-detail",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["last_relay"] is not None
    assert body["last_relay"]["via"] == "peer-relay"
    assert body["last_relay"]["ts"]  # non-empty


async def test_transport_detail_returns_null_when_no_recent_relay(client):
    r = await client.get(
        "/api/pairing/connections/peer-no-relay/transport-detail",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["last_relay"] is None


async def test_transport_detail_admin_only(client):
    """Non-admin user gets 403."""
    db = client.app[_db_key]
    await db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    r = await client.get(
        "/api/pairing/connections/peer-x/transport-detail",
        headers=_auth(client._tok),
    )
    assert r.status == 403


# ─── PATCH /api/pairing/connections/{instance_id} share_home ────────────────


class _CapturingShareHomeSvc:
    """Stub for PeerHomeSharingService that records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def set_share_home(
        self, instance_id: str, *, value: bool, set_by: str | None
    ) -> None:
        self.calls.append((instance_id, value, set_by))


async def test_patch_share_home_false_persists_and_calls_service(client):
    """PATCH {share_home: false} returns 200 and the service sets the value."""
    fed_repo = client.app[federation_repo_key]
    inst = _fake_instance("peer-sh-1")
    await fed_repo.save_instance(inst)

    stub = _CapturingShareHomeSvc()
    client.app[peer_home_sharing_service_key] = stub

    r = await client.patch(
        "/api/pairing/connections/peer-sh-1",
        json={"share_home": False},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["instance_id"] == "peer-sh-1"
    assert stub.calls == [("peer-sh-1", False, client._uid)]


async def test_patch_share_home_true_persists_and_calls_service(client):
    """PATCH {share_home: true} returns 200 and the service sets the value."""
    fed_repo = client.app[federation_repo_key]
    inst = _fake_instance("peer-sh-2")
    await fed_repo.save_instance(inst)

    stub = _CapturingShareHomeSvc()
    client.app[peer_home_sharing_service_key] = stub

    r = await client.patch(
        "/api/pairing/connections/peer-sh-2",
        json={"share_home": True},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["instance_id"] == "peer-sh-2"
    assert stub.calls == [("peer-sh-2", True, client._uid)]


async def test_patch_share_home_invalid_type_returns_400(client):
    """PATCH with a non-bool share_home returns 422 UNPROCESSABLE."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-sh-3"))

    r = await client.patch(
        "/api/pairing/connections/peer-sh-3",
        json={"share_home": "yes"},
        headers=_auth(client._tok),
    )
    assert r.status == 422
    body = await r.json()
    assert body["error"]["code"] == "UNPROCESSABLE"


async def test_patch_share_home_requires_admin(client):
    """Non-admin user is rejected with 403."""
    from socialhome.auth import sha256_token_hash
    from socialhome.crypto import derive_user_id

    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-sh-4"))

    db = client.app[_db_key]
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    pk_bytes = bytes.fromhex(row["identity_public_key"])
    uid = derive_user_id(pk_bytes, "member-sh")
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("member-sh", uid, "Member"),
    )
    raw = "member-sh-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-mem-sh", uid, "t", sha256_token_hash(raw)),
    )

    r = await client.patch(
        "/api/pairing/connections/peer-sh-4",
        json={"share_home": False},
        headers=_auth(raw),
    )
    assert r.status == 403


# ─── GET /api/connections includes share_home ───────────────────────────────


async def test_get_connections_includes_share_home(client):
    """GET /api/connections returns share_home on every row; default is True."""
    fed_repo = client.app[federation_repo_key]
    inst = _fake_instance("peer-sh-list-1")
    await fed_repo.save_instance(inst)

    r = await client.get(
        "/api/connections",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == "peer-sh-list-1")
    # Default value is True — RemoteInstance.share_home defaults to True.
    assert row["share_home"] is True


# ─── GET /api/connections includes queued_envelopes ────────────────────────


async def test_get_connections_reports_zero_queued_envelopes(client):
    """A peer with an empty outbox reports queued_envelopes=0.

    The field is always present so the SPA can branch on the number
    rather than on ``undefined``.
    """
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-q-empty"))

    r = await client.get("/api/connections", headers=_auth(client._tok))
    assert r.status == 200
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == "peer-q-empty")
    assert row["queued_envelopes"] == 0


async def test_get_connections_counts_pending_outbox_envelopes(client):
    """queued_envelopes reflects the peer's own pending outbox backlog.

    A household that has been offline for weeks piles up undelivered
    envelopes; the count is what tells an admin the difference between a
    blip and a peer that has been gone since spring.
    """
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-q-backlog"))
    await fed_repo.save_instance(_fake_instance("peer-q-other"))

    outbox = client.app[outbox_repo_key]
    for _ in range(3):
        await outbox.enqueue(
            instance_id="peer-q-backlog",
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload_json="{}",
        )
    await outbox.enqueue(
        instance_id="peer-q-other",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
    )

    r = await client.get("/api/connections", headers=_auth(client._tok))
    assert r.status == 200
    rows = await r.json()
    backlog = next(x for x in rows if x["instance_id"] == "peer-q-backlog")
    other = next(x for x in rows if x["instance_id"] == "peer-q-other")
    assert backlog["queued_envelopes"] == 3
    assert other["queued_envelopes"] == 1


async def test_delivered_envelopes_are_not_counted_as_queued(client):
    """Only ``pending`` rows count — delivered ones are not a backlog."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-q-delivered"))

    outbox = client.app[outbox_repo_key]
    entry_id = await outbox.enqueue(
        instance_id="peer-q-delivered",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
    )
    await outbox.mark_delivered(entry_id)

    r = await client.get("/api/connections", headers=_auth(client._tok))
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == "peer-q-delivered")
    assert row["queued_envelopes"] == 0


# ─── dropped (permanently failed) envelopes ────────────────────────────────


async def _seed_backlog(client, iid: str, *, pending: int, failed: int) -> None:
    """Give ``iid`` a real outbox backlog: ``pending`` waiting, ``failed`` dead."""
    outbox = client.app[outbox_repo_key]
    for i in range(pending):
        await outbox.enqueue(
            instance_id=iid,
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload_json="{}",
            msg_id=f"{iid}-p{i}",
        )
    for i in range(failed):
        eid = await outbox.enqueue(
            instance_id=iid,
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload_json="{}",
            msg_id=f"{iid}-f{i}",
        )
        await outbox.mark_failed(eid)


async def test_get_connections_reports_dropped_envelopes(client):
    """``failed`` rows are permanently given up on — reported separately.

    The support case that motivated this: 263 failed + 56 pending. Showing
    only the 56 renders data loss as a reassuring "queued for delivery".
    """
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-dropped"))
    await _seed_backlog(client, "peer-dropped", pending=2, failed=3)

    r = await client.get("/api/connections", headers=_auth(client._tok))
    assert r.status == 200
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == "peer-dropped")
    assert row["queued_envelopes"] == 2
    assert row["dropped_envelopes"] == 3


async def test_get_connections_reports_zero_dropped_envelopes(client):
    """The field is always present so the SPA branches on a number."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-nodrop"))

    r = await client.get("/api/connections", headers=_auth(client._tok))
    rows = await r.json()
    row = next(x for x in rows if x["instance_id"] == "peer-nodrop")
    assert row["dropped_envelopes"] == 0


async def test_patch_connection_reports_real_envelope_counts(client):
    """PATCH returns the peer's real backlog, not a placeholder ``0``."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_fake_instance("peer-patch-counts"))
    await _seed_backlog(client, "peer-patch-counts", pending=2, failed=3)
    client.app[peer_home_sharing_service_key] = _CapturingShareHomeSvc()

    r = await client.patch(
        "/api/pairing/connections/peer-patch-counts",
        json={"share_home": False},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["queued_envelopes"] == 2
    assert body["dropped_envelopes"] == 3


class _StubConfirmSvc:
    """Federation service stub whose confirm_pairing returns a known peer."""

    def __init__(self, inst) -> None:
        self._inst = inst

    async def confirm_pairing(self, token: str, code: str):
        return self._inst


async def test_confirm_pairing_reports_real_envelope_counts(client):
    """The confirm response carries the peer's real backlog, not ``0``.

    A re-confirm of a household that has been dark is exactly when the
    admin needs to see what piled up and what was dropped.
    """
    inst = _fake_instance("peer-confirm-counts")
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(inst)
    await _seed_backlog(client, "peer-confirm-counts", pending=2, failed=3)
    client.app[federation_service_key] = _StubConfirmSvc(inst)

    r = await client.post(
        "/api/pairing/confirm",
        json={"token": "t", "verification_code": "123456"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    body = await r.json()
    assert body["instance_id"] == "peer-confirm-counts"
    assert body["queued_envelopes"] == 2
    assert body["dropped_envelopes"] == 3
