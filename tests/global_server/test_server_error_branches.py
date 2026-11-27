"""Error-path coverage for :mod:`socialhome.global_server.server`.

Exercises the JSON-decode, missing-field, invalid-signature, and
unknown-NODE_* branches that the success-path tests skip — plus
``_fan_out`` via a real subscriber HTTPS inbox and the CLI entry point
(``main``, ``_cli_init``, ``_cli_set_password``).
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from dataclasses import replace
from datetime import UTC

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from PIL import Image

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


#: A closed loopback port — a delivery attempt is refused immediately, with no
#: DNS lookup and nothing leaving the host.


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
                status="active",
            )
        )
        tc._seed = seed
        tc._pub_hex = pub_hex
        tc._app = app
        yield tc


# ─── Helpers ───────────────────────────────────────────────────────────


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()


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

    _real_seed, real_pub = _gen_ed25519()
    await client._app[gfs_cluster_repo_key].approve_node(
        "peer", "http://peer", real_pub
    )
    wrong_seed, _ = _gen_ed25519()
    canonical, sig = _sign_canonical(
        {
            "type": "NODE_HEARTBEAT",
            "from": "peer",
            "ts": int(time.time()),
            "payload": {},
        },
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

    seed, pub = _gen_ed25519()
    await client._app[gfs_cluster_repo_key].approve_node("peer2", "http://peer2", pub)
    canonical, sig = _sign_canonical(
        {
            "type": "NODE_FLYING_SPAGHETTI",
            "from": "peer2",
            "ts": int(time.time()),
            "payload": {},
        },
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


# ─── Signed household → GFS request auth (``_rtc_authenticate``) ─────


async def test_rtc_authenticate_invalid_json_raises_400(client):
    resp = await client.post(
        "/gfs/highlight_rtc/answer",
        data=b"{not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_rtc_authenticate_missing_instance_id_is_422(client):
    resp = await client.post("/gfs/highlight_rtc/answer", json={"sdp": "x"})
    assert resp.status == 422


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/gfs/rtc/offer"),
        ("POST", "/gfs/rtc/answer"),
        ("POST", "/gfs/rtc/ice"),
        ("POST", "/gfs/rtc/ping"),
        ("GET", "/gfs/rtc/session/any"),
    ],
)
async def test_household_rtc_rendezvous_is_gone(client, method, path):
    """No Social Home release ever called the household-to-household
    rendezvous, and its sessions shared the anonymous highlight slots — so
    it is not served at all (#1112 follow-up)."""
    resp = await client.request(method, path, json={})
    assert resp.status == 404


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


async def test_header_image_between_1mib_and_2mib_passes_aiohttp_client_max_size(
    client,
):
    """Regression: aiohttp >= 3.13.3 (aio-libs/aiohttp#11889) makes
    ``BodyPartReader.read()`` 413 past the app's 1 MiB ``client_max_size``,
    so the view's own 2 MiB check never ran for a 1–2 MiB header image.
    The part is streamed under the 2 MiB cap now, so it reaches the image
    pipeline and succeeds."""
    # Random noise does not compress — 1700x1300 at q80 lands at ~1.5 MiB.
    img = Image.frombytes("RGB", (1700, 1300), os.urandom(1700 * 1300 * 3))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    data = buf.getvalue()
    assert 1_048_576 < len(data) < 2 * 1024 * 1024
    fd = FormData()
    fd.add_field("file", data, filename="hero.jpg", content_type="image/jpeg")
    resp = await client.post(
        "/admin/api/branding/header-image",
        data=fd,
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["header_image_file"].endswith(".webp")


async def test_header_image_upload_rejects_non_image(client):
    from aiohttp import FormData

    fd = FormData()
    fd.add_field("file", b"not an image", filename="bad.txt", content_type="text/plain")
    resp = await client.post(
        "/admin/api/branding/header-image",
        data=fd,
    )
    assert resp.status == 415


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

    def _fake_run_app(app, *, host, port, access_log_class):
        called["host"] = host
        called["port"] = port

    monkeypatch.setattr(
        "socialhome.global_server.server.web.run_app",
        _fake_run_app,
    )
    main()
    assert called["host"] != ""
    assert isinstance(called["port"], int)


# ─── Signed request auth: unknown sender + bad signature ─────────────


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
    resp = await client.post("/gfs/highlight_rtc/answer", json=body)
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
    resp = await client.post("/gfs/highlight_rtc/answer", json=body)
    assert resp.status == 401


# ─── Client-IP resolution on the admin audit trail ────────────────────


async def test_admin_audit_ip_uses_last_forwarded_entry_from_trusted_peer(client):
    """The TestServer peer is loopback — trusted by default — so the LAST
    ``X-Forwarded-For`` entry is the hop that proxy appended and becomes the
    audit IP. The client-supplied prefix must never be recorded: an operator
    reading the audit log would otherwise be reading attacker-chosen text."""
    resp = await client.post(
        "/admin/api/clients/peer.home/accept",
        headers={"X-Forwarded-For": "10.0.0.1, 10.0.0.2"},
    )
    assert resp.status == 200
    resp = await client.get("/admin/api/audit?action=accept_client")
    ips = {r.get("admin_ip") for r in await resp.json()}
    assert "10.0.0.2" in ips
    assert "10.0.0.1" not in ips


async def test_admin_audit_ip_ignores_forwarded_for_from_untrusted_peer(tmp_dir):
    """With no trusted proxies the header is inert — the audit log records the
    real TCP peer, not whatever the caller claimed."""
    app = create_gfs_app(replace(_config(tmp_dir), trusted_proxies=()))
    async with TestClient(TestServer(app)) as tc:
        await app[gfs_admin_repo_key].set_config(
            "admin_password_hash",
            hash_password("admin-pw"),
        )
        await tc.post("/admin/login", json={"password": "admin-pw"})
        _seed, pub_hex = _gen_ed25519()
        await app[gfs_fed_repo_key].upsert_instance(
            ClientInstance(
                instance_id="peer.home",
                display_name="Peer",
                public_key=pub_hex,
                status="active",
            )
        )
        resp = await tc.post(
            "/admin/api/clients/peer.home/accept",
            headers={"X-Forwarded-For": "10.0.0.1, 10.0.0.2"},
        )
        assert resp.status == 200
        resp = await tc.get("/admin/api/audit?action=accept_client")
        ips = {r.get("admin_ip") for r in await resp.json()}
        assert ips == {"127.0.0.1"}


# ─── Channel publish input bounds ─────────────────────────────────────


async def test_channel_publish_rejects_oversized_declared_body(client):
    """A ``Content-Length`` over the cap is a 413 BEFORE the body is read."""
    from socialhome.global_server.public import PUBLISH_MAX_BODY_BYTES

    oversized = b"x" * (PUBLISH_MAX_BODY_BYTES + 1)
    resp = await client.post(
        "/gfs/channels/publish-anon",
        data=oversized,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 413


async def test_channel_publish_rejects_oversized_chunked_body(client):
    """A chunked body has no ``Content-Length`` — the read itself is bounded."""
    from socialhome.global_server.public import PUBLISH_MAX_BODY_BYTES

    async def _chunks():
        for _ in range((PUBLISH_MAX_BODY_BYTES // 4096) + 2):
            yield b"x" * 4096

    resp = await client.post(
        "/gfs/channels/publish-anon",
        data=_chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 413
