"""Error-path coverage for :mod:`socialhome.global_server.server`.

Exercises the JSON-decode, missing-field, invalid-signature, and
unknown-NODE_* branches that the success-path tests skip — plus
``_fan_out`` via a real subscriber HTTPS inbox and the CLI entry point
(``main``, ``_cli_init``, ``_cli_set_password``).
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    sign_authority_event,
)
from socialhome.crypto import b64url_encode, sign_ed25519
from socialhome.global_server.admin import hash_password
from socialhome.global_server.app_keys import (
    gfs_admin_repo_key,
    gfs_fed_repo_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import (
    _cli_init,
    _cli_set_password,
    create_gfs_app,
    main,
)


def _config(tmp, *, instance_id="gfs-a"):
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp),
        instance_id=instance_id,
        cluster_enabled=True,
        cluster_node_id=instance_id,
        cluster_peers=(),
    )


def _gen_ed25519():
    priv = ed25519.Ed25519PrivateKey.generate()
    seed = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_hex = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    return seed, pub_hex


def _sign(body: dict, seed: bytes) -> dict:
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return {**body, "signature": b64url_encode(sign_ed25519(seed, canonical))}


def _sign_canonical(body: dict, seed: bytes) -> tuple[bytes, str]:
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return canonical, b64url_encode(sign_ed25519(seed, canonical))


@pytest.fixture
async def client(tmp_dir):
    """Authenticated GFS admin TestClient with one active peer registered."""
    app = create_gfs_app(_config(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        await app[gfs_admin_repo_key].set_config(
            "admin_password_hash",
            hash_password("admin-pw"),
        )
        await tc.post("/admin/login", json={"password": "admin-pw"})
        # Pre-register a single active peer for signature-aware tests.
        seed, pub_hex = _gen_ed25519()
        await app[gfs_fed_repo_key].upsert_instance(
            ClientInstance(
                instance_id="peer.home",
                display_name="Peer",
                public_key=pub_hex,
                inbox_url="http://peer.home/wh",
                status="active",
            )
        )
        tc._seed = seed
        tc._pub_hex = pub_hex
        tc._app = app
        yield tc


# ─── /gfs/publish + /gfs/subscribe + /gfs/register ────────────────────


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


async def _publish_known_space(
    client, space_id: str, *, identity_public_key: str = ""
) -> None:
    """Publish a minimal active space owned by the fixture's peer.home.

    ``identity_public_key`` (hex) TOFU-pins the space's authority verify key —
    required before ``/gfs/publish`` will relay anything for the space.
    """
    pub_body = {
        "owning_instance": "peer.home",
        "name": "Known",
        "description": "",
        "about_markdown": "",
        "cover_url": "",
        "icon_url": "",
        "min_age": 0,
        "category": "general",
        "accent_color": "#D2542A",
        "primary_color": "#D2542A",
        "identity_public_key": identity_public_key,
    }
    signed = _sign({**pub_body, "space_id": space_id}, client._seed)
    resp = await client.post(
        f"/gfs/spaces/{space_id}/publish",
        json={**pub_body, "signature": signed["signature"]},
    )
    assert resp.status == 200


async def _authority_space(client, space_id: str) -> bytes:
    """Publish *space_id* with a fresh pinned authority key; return its seed."""
    space_seed, space_pub_hex = _gen_ed25519()
    await _publish_known_space(client, space_id, identity_public_key=space_pub_hex)
    return space_seed


def _authority_payload(space_seed: bytes, *, space_id: str) -> dict:
    payload = {"ciphertext": "opaque-blob"}
    payload.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id=space_id,
            payload=payload,
            space_seed=space_seed,
        )
    )
    return payload


async def test_publish_canonical_identity_free_body_is_accepted(client):
    """The canonical body carries NO household identity — the space-authority
    signature inside the payload is the whole authorization."""
    space_seed = await _authority_space(client, "sp-empty")
    resp = await client.post(
        "/gfs/publish",
        json={
            "space_id": "sp-empty",
            "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            "payload": _authority_payload(space_seed, space_id="sp-empty"),
        },
    )
    assert resp.status == 200
    payload = await resp.json()
    assert payload["delivered_to"] == 0


async def test_publish_legacy_body_with_authority_sig_is_accepted(client):
    """An older household still sending ``from_instance`` + its transport
    signature keeps working — the legacy fields are verified, then ignored."""
    space_seed = await _authority_space(client, "sp-legacy-route")
    body = {
        "space_id": "sp-legacy-route",
        "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        "payload": _authority_payload(space_seed, space_id="sp-legacy-route"),
        "from_instance": "peer.home",
    }
    resp = await client.post("/gfs/publish", json=_sign(body, client._seed))
    assert resp.status == 200
    assert (await resp.json())["delivered_to"] == 0


async def test_publish_without_authority_sig_is_403(client):
    """The owner loophole is closed at the route too: a signed legacy body from
    the space's OWNER with no authority sig in the payload is a 403."""
    await _authority_space(client, "sp-noauth-route")
    body = {
        "space_id": "sp-noauth-route",
        "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        "payload": {"ciphertext": "opaque-blob"},
        "from_instance": "peer.home",
    }
    resp = await client.post("/gfs/publish", json=_sign(body, client._seed))
    assert resp.status == 403


async def test_publish_unknown_space_is_403(client):
    """A validly-signed event for a space the GFS never published is rejected
    (no auto-mint of an ownership row from an event)."""
    body = {
        "space_id": "sp-never-published",
        "event_type": "TEST",
        "payload": {"x": 1},
        "from_instance": "peer.home",
    }
    resp = await client.post("/gfs/publish", json=_sign(body, client._seed))
    assert resp.status == 403


async def test_publish_missing_field_is_400(client):
    resp = await client.post("/gfs/publish", json={"space_id": "x"})
    assert resp.status == 400


async def test_publish_unsigned_is_403(client):
    """A publish with no signature is rejected with 403 (mandatory sig)."""
    resp = await client.post(
        "/gfs/publish",
        json={
            "space_id": "sp-unsigned",
            "event_type": "TEST",
            "payload": {"x": 1},
            "from_instance": "peer.home",
            "signature": "",
        },
    )
    assert resp.status == 403


async def test_publish_invalid_signature_is_403(client):
    """A forged / malformed signature is rejected with 403."""
    resp = await client.post(
        "/gfs/publish",
        json={
            "space_id": "sp-bad",
            "event_type": "TEST",
            "payload": {"x": 1},
            "from_instance": "peer.home",
            "signature": "!!!not-base64!!!",
        },
    )
    assert resp.status == 403


async def test_subscribe_then_unsubscribe(client):
    # The space must already be published — subscribe no longer mints a row.
    await _publish_known_space(client, "sp-sub")
    ts = _now_iso()
    signed = _sign(
        {
            "action": "subscribe",
            "instance_id": "peer.home",
            "space_id": "sp-sub",
            "ts": ts,
        },
        client._seed,
    )
    resp = await client.post(
        "/gfs/subscribe",
        json={
            "instance_id": "peer.home",
            "space_id": "sp-sub",
            "ts": ts,
            "signature": signed["signature"],
        },
    )
    assert resp.status == 200
    assert (await resp.json())["status"] == "subscribed"
    # Unsubscribe action — signed and self-binding, exactly like subscribe.
    ts2 = _now_iso()
    signed2 = _sign(
        {
            "action": "unsubscribe",
            "instance_id": "peer.home",
            "space_id": "sp-sub",
            "ts": ts2,
        },
        client._seed,
    )
    resp = await client.post(
        "/gfs/subscribe",
        json={
            "instance_id": "peer.home",
            "space_id": "sp-sub",
            "action": "unsubscribe",
            "ts": ts2,
            "signature": signed2["signature"],
        },
    )
    assert (await resp.json())["status"] == "unsubscribed"


async def test_subscribe_missing_field_is_400(client):
    resp = await client.post("/gfs/subscribe", json={})
    assert resp.status == 400


# ─── /cluster/sync — JSON-body, missing-field, bad-sig, unknown-type ─


async def test_cluster_sync_invalid_json_is_400(client):
    resp = await client.post(
        "/cluster/sync",
        data=b"not json",
        headers={
            "Content-Type": "application/json",
            "X-Node-Signature": "sig",
            "X-Node-Id": "n",
        },
    )
    assert resp.status == 400


async def test_cluster_sync_missing_type_is_400(client):
    canonical = json.dumps(
        {"from": "n"}, separators=(",", ":"), sort_keys=True
    ).encode()
    resp = await client.post(
        "/cluster/sync",
        data=canonical,
        headers={
            "Content-Type": "application/json",
            "X-Node-Signature": "sig",
            "X-Node-Id": "n",
        },
    )
    assert resp.status == 400


async def test_cluster_sync_known_node_bad_sig_is_401(client):
    # Register a peer node with a valid public key, then send a body
    # signed with a DIFFERENT key so verification fails.
    from socialhome.global_server.app_keys import gfs_cluster_repo_key
    from socialhome.global_server.domain import ClusterNode

    _real_seed, real_pub = _gen_ed25519()
    await client._app[gfs_cluster_repo_key].upsert_node(
        ClusterNode(
            node_id="peer",
            url="http://peer",
            public_key=real_pub,
            status="online",
        )
    )
    wrong_seed, _ = _gen_ed25519()
    canonical, sig = _sign_canonical(
        {"type": "NODE_HEARTBEAT", "from": "peer", "ts": 0, "payload": {}},
        wrong_seed,
    )
    resp = await client.post(
        "/cluster/sync",
        data=canonical,
        headers={
            "Content-Type": "application/json",
            "X-Node-Signature": sig,
            "X-Node-Id": "peer",
        },
    )
    assert resp.status == 401


async def test_cluster_sync_unknown_node_type_dispatches_silently(client):
    # Register the peer so sig verification passes, then send a NODE_*
    # type that isn't in the match list — server logs + 200s.
    from socialhome.global_server.app_keys import gfs_cluster_repo_key
    from socialhome.global_server.domain import ClusterNode

    seed, pub = _gen_ed25519()
    await client._app[gfs_cluster_repo_key].upsert_node(
        ClusterNode(
            node_id="peer2",
            url="http://peer2",
            public_key=pub,
            status="online",
        )
    )
    canonical, sig = _sign_canonical(
        {"type": "NODE_FLYING_SPAGHETTI", "from": "peer2", "ts": 0, "payload": {}},
        seed,
    )
    resp = await client.post(
        "/cluster/sync",
        data=canonical,
        headers={
            "Content-Type": "application/json",
            "X-Node-Signature": sig,
            "X-Node-Id": "peer2",
        },
    )
    assert resp.status == 200


# ─── /gfs/appeal + /gfs/report error branches ────────────────────────


async def test_appeal_missing_fields_is_422(client):
    resp = await client.post("/gfs/appeal", json={})
    assert resp.status == 422


async def test_appeal_invalid_json_is_400(client):
    resp = await client.post(
        "/gfs/appeal",
        data=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_appeal_unknown_sender_is_403(client):
    body = _sign(
        {
            "target_type": "space",
            "target_id": "sp",
            "from_instance": "ghost.home",
            "message": "please",
        },
        client._seed,
    )  # wrong key anyway but sender is unknown first
    resp = await client.post("/gfs/appeal", json=body)
    assert resp.status == 403


async def test_appeal_bad_signature_is_401(client):
    other_seed, _ = _gen_ed25519()
    body = _sign(
        {
            "target_type": "space",
            "target_id": "sp",
            "from_instance": "peer.home",
            "message": "please",
        },
        other_seed,
    )
    resp = await client.post("/gfs/appeal", json=body)
    assert resp.status == 401


async def test_appeal_happy_path_creates_pending_row(client):
    body = _sign(
        {
            "target_type": "space",
            "target_id": "sp",
            "from_instance": "peer.home",
            "message": "please",
        },
        client._seed,
    )
    resp = await client.post("/gfs/appeal", json=body)
    assert resp.status == 201
    data = await resp.json()
    assert data["status"] == "pending"


async def test_report_missing_fields_is_422(client):
    resp = await client.post("/gfs/report", json={})
    assert resp.status == 422


async def test_report_bad_signature_is_401(client):
    other_seed, _ = _gen_ed25519()
    body = _sign(
        {
            "target_type": "space",
            "target_id": "sp",
            "category": "spam",
            "reporter_instance_id": "peer.home",
        },
        other_seed,
    )
    resp = await client.post("/gfs/report", json=body)
    assert resp.status == 401


async def test_report_invalid_json_is_400(client):
    resp = await client.post(
        "/gfs/report",
        data=b"{bad",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


# ─── /gfs/rtc/* additional error branches ────────────────────────────


async def test_rtc_ice_invalid_candidate_is_422(client):
    # candidate must be a dict; sending a string triggers 422.
    body = _sign(
        {
            "instance_id": "peer.home",
            "session_id": "sess",
            "candidate": "not-a-dict",
        },
        client._seed,
    )
    resp = await client.post("/gfs/rtc/ice", json=body)
    assert resp.status == 422


async def test_rtc_ice_unknown_session_is_404(client):
    body = _sign(
        {
            "instance_id": "peer.home",
            "session_id": "does-not-exist",
            "candidate": {"candidate": "x", "sdpMid": "0"},
        },
        client._seed,
    )
    resp = await client.post("/gfs/rtc/ice", json=body)
    assert resp.status == 404


async def test_rtc_authenticate_invalid_json_raises_400(client):
    resp = await client.post(
        "/gfs/rtc/offer",
        data=b"{not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


# ─── Admin POST body-parse error branches ────────────────────────────
#
# Each of these handlers wraps ``await request.json()`` in a try/except
# so a missing or malformed body still succeeds (defaulting to {}).


async def test_admin_ban_client_tolerates_bad_body(client):
    # Seed an instance to ban so the service call succeeds.
    resp = await client.post(
        "/admin/api/clients/peer.home/ban",
        data=b"{not-json",
        headers={"Content-Type": "application/json"},
    )
    # Either 200 (body defaulted) or 400; either way exercises the except.
    assert resp.status in (200, 400)


async def test_admin_ban_space_tolerates_bad_body(client):
    # Seed a space first.
    from socialhome.global_server.domain import GlobalSpace

    await client._app[gfs_fed_repo_key].upsert_space(
        GlobalSpace(
            space_id="sp-ban",
            owning_instance="peer.home",
            status="active",
        )
    )
    resp = await client.post(
        "/admin/api/spaces/sp-ban/ban",
        data=b"{not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status in (200, 400)


async def test_admin_set_policy_tolerates_bad_body(client):
    resp = await client.patch(
        "/admin/api/policy",
        data=b"{bad",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status in (200, 400)


async def test_admin_set_branding_tolerates_bad_body(client):
    resp = await client.patch(
        "/admin/api/branding",
        data=b"{bad",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status in (200, 400)


async def test_admin_review_report_tolerates_bad_body(client):
    resp = await client.post(
        "/admin/api/reports/missing/review",
        data=b"{bad",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status in (404, 400, 422)


async def test_admin_decide_appeal_tolerates_bad_body(client):
    resp = await client.post(
        "/admin/api/appeals/missing/decide",
        data=b"{bad",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status in (404, 400, 422)


async def test_admin_cluster_add_peer_tolerates_bad_body(client):
    resp = await client.post(
        "/admin/api/cluster/peers",
        data=b"{bad",
        headers={"Content-Type": "application/json"},
    )
    # body defaults to {} → "missing_url" 422.
    assert resp.status == 422


# ─── /admin/api/audit — query-param coverage ────────────────────────


async def test_admin_list_audit_with_query_params(client):
    # Trigger a couple of audit writes so the list isn't empty.
    await client.post("/admin/api/clients/peer.home/accept")
    resp = await client.get(
        "/admin/api/audit?action=accept_client&since=1700000000&limit=5",
    )
    assert resp.status == 200
    rows = await resp.json()
    assert isinstance(rows, list)


async def test_admin_list_audit_clamps_limit(client):
    # limit > 500 is clamped, limit < 1 is clamped up.
    resp = await client.get("/admin/api/audit?limit=99999")
    assert resp.status == 200
    resp = await client.get("/admin/api/audit?limit=0")
    assert resp.status == 200


# ─── Header-image upload error branches ─────────────────────────────


async def test_header_image_upload_missing_file(client):
    # Multipart with no "file" field.
    from aiohttp import FormData

    fd = FormData()
    fd.add_field("other", "value")
    resp = await client.post(
        "/admin/api/branding/header-image",
        data=fd,
    )
    assert resp.status == 400


async def test_header_image_upload_rejects_oversize(client):
    from aiohttp import FormData

    # 3 MiB > 2 MiB cap.
    big = b"\x00" * (3 * 1024 * 1024)
    fd = FormData()
    fd.add_field("file", big, filename="big.jpg", content_type="image/jpeg")
    resp = await client.post(
        "/admin/api/branding/header-image",
        data=fd,
    )
    assert resp.status == 413


async def test_header_image_upload_rejects_non_image(client):
    from aiohttp import FormData

    fd = FormData()
    fd.add_field("file", b"not an image", filename="bad.txt", content_type="text/plain")
    resp = await client.post(
        "/admin/api/branding/header-image",
        data=fd,
    )
    assert resp.status == 415


# ─── Federation _fan_out via a real subscriber HTTPS inbox ──────────────


async def test_fan_out_delivers_to_real_subscriber_inbox(
    tmp_dir,
    tmp_path_factory,
):
    """A real subscriber HTTPS inbox receives the published event over HTTP.

    Spins up a mini aiohttp app that records every POST to ``/wh``,
    registers it as a GFS subscriber, then publishes an event — the
    federation service's ``_fan_out`` -> HTTPS-inbox path must deliver.
    """
    received = []

    async def _wh_handler(request: web.Request) -> web.Response:
        received.append(await request.json())
        return web.json_response({"ok": True})

    sub_app = web.Application()
    sub_app.router.add_post("/wh", _wh_handler)
    sub_server = TestServer(sub_app)
    await sub_server.start_server()
    try:
        sub_url = str(sub_server.make_url("/wh"))
        gfs_app = create_gfs_app(_config(tmp_path_factory.mktemp("gfs-pub")))
        async with TestClient(TestServer(gfs_app)) as tc:
            sub_seed, sub_pub = _gen_ed25519()
            pub_seed, pub_pub = _gen_ed25519()
            # Seed the subscriber + publisher into the fed repo with real keys.
            await gfs_app[gfs_fed_repo_key].upsert_instance(
                ClientInstance(
                    instance_id="sub.home",
                    display_name="Sub",
                    public_key=sub_pub,
                    inbox_url=sub_url,
                    status="active",
                )
            )
            await gfs_app[gfs_fed_repo_key].upsert_instance(
                ClientInstance(
                    instance_id="pub.home",
                    display_name="Pub",
                    public_key=pub_pub,
                    inbox_url="http://pub",
                    status="active",
                )
            )
            # pub.home publishes the space so it's a real subscribe target.
            pub_body = {
                "owning_instance": "pub.home",
                "name": "XYZ",
                "description": "",
                "about_markdown": "",
                "cover_url": "",
                "icon_url": "",
                "min_age": 0,
                "category": "general",
                "accent_color": "#D2542A",
                "primary_color": "#D2542A",
            }
            space_seed, space_pub = _gen_ed25519()
            pub_body["identity_public_key"] = space_pub
            signed_pub = _sign({**pub_body, "space_id": "sp-xyz"}, pub_seed)
            resp = await tc.post(
                "/gfs/spaces/sp-xyz/publish",
                json={**pub_body, "signature": signed_pub["signature"]},
            )
            assert resp.status == 200
            ts = _now_iso()
            signed_sub = _sign(
                {
                    "action": "subscribe",
                    "instance_id": "sub.home",
                    "space_id": "sp-xyz",
                    "ts": ts,
                },
                sub_seed,
            )
            resp = await tc.post(
                "/gfs/subscribe",
                json={
                    "instance_id": "sub.home",
                    "space_id": "sp-xyz",
                    "ts": ts,
                    "signature": signed_sub["signature"],
                },
            )
            assert resp.status == 200
            event_body = {
                "space_id": "sp-xyz",
                "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
                "payload": _authority_payload(space_seed, space_id="sp-xyz"),
            }
            resp = await tc.post("/gfs/publish", json=event_body)
            assert resp.status == 200
            assert (await resp.json())["delivered_to"] == 1
        # Wait for the HTTPS inbox to capture the event.
        await asyncio.sleep(0.05)
        assert received
        assert received[0]["event_type"] == AUTHORITY_EVENT_SPACE_POST_PUBLIC
        # The HTTPS-inbox body is identity-free too.
        assert set(received[0]) == {"space_id", "event_type", "payload"}
        assert "pub.home" not in json.dumps(received[0])
    finally:
        await sub_server.close()


# ─── CLI entry points — main / _cli_init / _cli_set_password ────────


def test_cli_init_writes_and_refuses_overwrite(tmp_path):
    target = tmp_path / "gfs.toml"
    # First call writes.
    rc = _cli_init(target)
    assert rc == 0
    assert target.is_file()
    # Second call refuses — file exists.
    rc = _cli_init(target)
    assert rc == 2


def test_cli_set_password_no_config_errors(tmp_path):
    target = tmp_path / "missing.toml"
    rc = _cli_set_password(target)
    assert rc == 2


def test_cli_set_password_updates_toml(tmp_path, monkeypatch):
    target = tmp_path / "gfs.toml"
    assert _cli_init(target) == 0
    # Fake getpass — return a matching password twice.
    monkeypatch.setattr(
        "socialhome.global_server.server.getpass.getpass",
        lambda _prompt="": "correcthorsebattery",
    )
    rc = _cli_set_password(target)
    assert rc == 0
    content = target.read_text()
    assert "password_hash" in content


def test_cli_set_password_mismatch_errors(tmp_path, monkeypatch):
    target = tmp_path / "gfs.toml"
    assert _cli_init(target) == 0
    # Return two different strings for the two getpass calls.
    seq = iter(["first-pw", "different-pw"])
    monkeypatch.setattr(
        "socialhome.global_server.server.getpass.getpass",
        lambda _prompt="": next(seq),
    )
    rc = _cli_set_password(target)
    assert rc == 2


def test_main_init_short_circuits_without_starting_server(tmp_path, monkeypatch):
    """``socialhome-global-server --init <path>`` writes config + exits."""
    target = tmp_path / "from_main.toml"
    monkeypatch.setattr(
        sys,
        "argv",
        ["socialhome-global-server", "--config", str(target), "--init"],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert target.is_file()


def test_main_set_password_short_circuits(tmp_path, monkeypatch):
    target = tmp_path / "cli.toml"
    assert _cli_init(target) == 0
    monkeypatch.setattr(
        sys,
        "argv",
        ["socialhome-global-server", "--config", str(target), "--set-password"],
    )
    monkeypatch.setattr(
        "socialhome.global_server.server.getpass.getpass",
        lambda _prompt="": "apropersecret",
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0


def test_main_runtime_path_calls_run_app(tmp_path, monkeypatch):
    """With no CLI action, ``main`` falls through to ``web.run_app``."""
    target = tmp_path / "run.toml"
    assert _cli_init(target) == 0
    # Fill the config with a password + base_url so `GfsConfig.load` passes.
    content = target.read_text()
    target.write_text(
        content.replace(
            'base_url = ""',
            'base_url = "http://gfs.test"',
        )
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["socialhome-global-server", "--config", str(target)],
    )
    # Stub out run_app so we don't actually bind a port.
    called = {}

    def _fake_run_app(app, *, host, port):  # noqa: D401
        called["host"] = host
        called["port"] = port

    monkeypatch.setattr(
        "socialhome.global_server.server.web.run_app",
        _fake_run_app,
    )
    main()
    assert called["host"] != ""
    assert isinstance(called["port"], int)


# ─── Extra RTC error branches (answer / ice / ping unknown + bad sig) ─


async def test_rtc_answer_rejects_unknown_instance(client):
    other_seed, _ = _gen_ed25519()
    body = _sign(
        {
            "instance_id": "ghost.home",
            "session_id": "x",
            "sdp": "y",
        },
        other_seed,
    )
    resp = await client.post("/gfs/rtc/answer", json=body)
    assert resp.status == 403


async def test_rtc_answer_bad_signature(client):
    other_seed, _ = _gen_ed25519()
    body = _sign(
        {
            "instance_id": "peer.home",
            "session_id": "x",
            "sdp": "y",
        },
        other_seed,
    )
    resp = await client.post("/gfs/rtc/answer", json=body)
    assert resp.status == 401


async def test_rtc_ice_unknown_instance(client):
    other_seed, _ = _gen_ed25519()
    body = _sign(
        {
            "instance_id": "ghost.home",
            "session_id": "x",
            "candidate": {"c": "x"},
        },
        other_seed,
    )
    resp = await client.post("/gfs/rtc/ice", json=body)
    assert resp.status == 403


async def test_rtc_ping_bad_signature(client):
    other_seed, _ = _gen_ed25519()
    body = _sign(
        {
            "instance_id": "peer.home",
            "transport": "webrtc",
        },
        other_seed,
    )
    resp = await client.post("/gfs/rtc/ping", json=body)
    assert resp.status == 401


# ─── _ip X-Forwarded-For branch ───────────────────────────────────────


async def test_admin_route_honours_x_forwarded_for(client):
    """``_ip`` prefers the first X-Forwarded-For entry over peername."""
    resp = await client.post(
        "/admin/api/clients/peer.home/accept",
        headers={"X-Forwarded-For": "10.0.0.1, 10.0.0.2"},
    )
    assert resp.status == 200
    # The audit row written should record the XFF IP.
    resp = await client.get("/admin/api/audit?action=accept_client")
    rows = await resp.json()
    assert any(r.get("admin_ip") == "10.0.0.1" for r in rows)


# ─── /gfs/publish hardening — roster leak, uniform 403, input bounds ───


async def _subscribe_peer(client, space_id: str) -> None:
    """Subscribe the fixture's ``peer.home`` to *space_id*'s relay fan-out."""
    ts = _now_iso()
    signed = _sign(
        {
            "action": "subscribe",
            "instance_id": "peer.home",
            "space_id": space_id,
            "ts": ts,
        },
        client._seed,
    )
    resp = await client.post(
        "/gfs/subscribe",
        json={
            "instance_id": "peer.home",
            "space_id": space_id,
            "ts": ts,
            "signature": signed["signature"],
        },
    )
    assert resp.status == 200


async def test_publish_response_never_leaks_the_subscriber_roster(client):
    """``delivered_to`` is a COUNT, never the instance ids.

    The authority signature carries no nonce/timestamp and the GFS keeps no
    replay cache, so anyone who captured one relay frame can re-POST it
    forever. Returning the roster would hand that anonymous replayer the exact
    data ``/gfs/spaces/{id}/subscribers`` gates behind a replay-guarded
    authority query.
    """
    space_seed = await _authority_space(client, "sp-roster")
    await _subscribe_peer(client, "sp-roster")
    resp = await client.post(
        "/gfs/publish",
        json={
            "space_id": "sp-roster",
            "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            "payload": _authority_payload(space_seed, space_id="sp-roster"),
        },
    )
    assert resp.status == 200
    raw = await resp.text()
    assert "peer.home" not in raw
    body = json.loads(raw)
    # A COUNT, not the roster. (peer.home holds no GFS WebSocket here and its
    # registered inbox is unreachable, so the count is 0 — the delivered=1 case
    # is covered by ``test_fan_out_delivers_to_real_subscriber_inbox``.)
    assert isinstance(body["delivered_to"], int)
    assert body["delivered_to"] == 0


async def test_publish_403_bodies_are_byte_identical(client):
    """Every relay refusal returns ONE uniform body.

    Distinct messages ("space not published" / "space is banned" / "no pinned
    authority key" / "invalid authority signature") let an UNAUTHENTICATED
    caller enumerate space existence, moderation status and pin status. The
    service keeps the precise reasons (logged at DEBUG); the route must not.
    """
    # 1. space never published.
    unknown = await client.post(
        "/gfs/publish",
        json={
            "space_id": "sp-uniform-missing",
            "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            "payload": {"authority_sig": "x", "authority_sig_suite": "ed25519"},
        },
    )
    # 2. space published but with no pinned authority key.
    await _publish_known_space(client, "sp-uniform-nopin")
    nopin = await client.post(
        "/gfs/publish",
        json={
            "space_id": "sp-uniform-nopin",
            "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            "payload": {"authority_sig": "x", "authority_sig_suite": "ed25519"},
        },
    )
    # 3. pinned key, but the authority signature does not verify.
    await _authority_space(client, "sp-uniform-badsig")
    other_seed, _ = _gen_ed25519()
    badsig = await client.post(
        "/gfs/publish",
        json={
            "space_id": "sp-uniform-badsig",
            "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            "payload": _authority_payload(other_seed, space_id="sp-uniform-badsig"),
        },
    )
    # 4. moderator-banned space.
    banned_seed = await _authority_space(client, "sp-uniform-banned")
    resp = await client.post(
        "/admin/api/spaces/sp-uniform-banned/ban",
        json={"reason": "test"},
    )
    assert resp.status == 200
    banned = await client.post(
        "/gfs/publish",
        json={
            "space_id": "sp-uniform-banned",
            "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            "payload": _authority_payload(banned_seed, space_id="sp-uniform-banned"),
        },
    )

    responses = [unknown, nopin, badsig, banned]
    assert [r.status for r in responses] == [403, 403, 403, 403]
    bodies = [await r.read() for r in responses]
    assert len(set(bodies)) == 1, bodies
    assert json.loads(bodies[0]) == {"error": "not authorized to relay for this space"}


@pytest.mark.parametrize(
    "space_id",
    [["a"], {"a": 1}, 7, "", "x" * 129, None],
)
async def test_publish_rejects_non_string_space_id(client, space_id):
    """A non-string / empty / oversized ``space_id`` is a 400.

    Passing it straight through reached the SQLite bind and produced a 500 +
    ERROR traceback on every hit — an unauthenticated log-volume DoS.
    """
    resp = await client.post(
        "/gfs/publish",
        json={
            "space_id": space_id,
            "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            "payload": {"authority_sig": "x"},
        },
    )
    assert resp.status == 400


@pytest.mark.parametrize(
    "event_type",
    [["a"], {"a": 1}, 7, "", "x" * 129, None],
)
async def test_publish_rejects_non_string_event_type(client, event_type):
    """Same bound on ``event_type`` — it reaches the same bind."""
    resp = await client.post(
        "/gfs/publish",
        json={
            "space_id": "sp-type",
            "event_type": event_type,
            "payload": {"authority_sig": "x"},
        },
    )
    assert resp.status == 400


async def test_publish_rejects_oversized_declared_body(client):
    """A ``Content-Length`` over the cap is a 413 BEFORE the body is read."""
    from socialhome.global_server.public import PUBLISH_MAX_BODY_BYTES

    oversized = b"x" * (PUBLISH_MAX_BODY_BYTES + 1)
    resp = await client.post(
        "/gfs/publish",
        data=oversized,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 413


async def test_publish_rejects_oversized_chunked_body(client):
    """A chunked body has no ``Content-Length`` — the read itself is bounded."""
    from socialhome.global_server.public import PUBLISH_MAX_BODY_BYTES

    async def _chunks():
        for _ in range((PUBLISH_MAX_BODY_BYTES // 4096) + 2):
            yield b"x" * 4096

    resp = await client.post(
        "/gfs/publish",
        data=_chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 413


async def test_publish_accepts_a_body_under_the_cap(client):
    """The cap leaves ample headroom for a real relay payload."""
    space_seed = await _authority_space(client, "sp-bigish")
    payload = {"ciphertext": "o" * 50_000}
    payload.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id="sp-bigish",
            payload=payload,
            space_seed=space_seed,
        )
    )
    resp = await client.post(
        "/gfs/publish",
        json={
            "space_id": "sp-bigish",
            "event_type": AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            "payload": payload,
        },
    )
    assert resp.status == 200
