"""Tests for ``GET /api/admin/diagnostics`` — the support bundle.

The bundle exists so an operator can attach one file to a bug report
instead of pasting log fragments. Its defining property is that it must be
*safe to share*, so most of these tests are about what is absent.
"""

from __future__ import annotations

import json

from socialhome.security import SENSITIVE_FIELDS

from .conftest import _auth


async def test_diagnostics_requires_admin(client):
    db = client._db
    await db.enqueue("UPDATE users SET is_admin=0 WHERE user_id=?", (client._uid,))
    resp = await client.get("/api/admin/diagnostics", headers=_auth(client._tok))
    assert resp.status == 403


async def test_diagnostics_has_the_sections_a_bug_report_needs(client):
    resp = await client.get("/api/admin/diagnostics", headers=_auth(client._tok))
    assert resp.status == 200
    body = await resp.json()

    # Each of these answered a real question while debugging a production
    # federation outage from a log alone.
    assert body["build"]["version"]
    assert body["build"]["proto_version"]
    assert "mode" in body["deployment"]
    assert "federation_base_configured" in body["deployment"]
    assert isinstance(body["peers"], list)
    assert isinstance(body["outbox"], list)
    assert "ice_servers" in body["webrtc"]
    assert "migration_version" in body["database"]
    assert body["generated_at"]
    # Versioned, so a future reader knows which shape they have.
    assert body["schema"] == 1


async def test_diagnostics_contains_no_sensitive_field_name(client):
    """The invariant the bundle lives or dies by.

    Asserted against the whole serialised payload and the full
    ``SENSITIVE_FIELDS`` frozenset, rather than the handful of keys I
    happened to think of — so a section added later that drags in a
    private key or a push endpoint fails here.
    """
    resp = await client.get("/api/admin/diagnostics", headers=_auth(client._tok))
    raw = await resp.text()
    body = json.loads(raw)

    def _keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from _keys(v)
        elif isinstance(node, list):
            for item in node:
                yield from _keys(item)

    present = set(_keys(body))
    assert not (present & SENSITIVE_FIELDS), (
        f"bundle exposes sensitive field(s): {sorted(present & SENSITIVE_FIELDS)}"
    )


async def test_diagnostics_omits_peer_inbox_paths_and_names(client):
    """A peer's inbox path embeds a per-pair secret; its display name is
    user-authored text. Neither belongs in a shareable file."""
    from socialhome.domain.federation import (
        InstanceSource,
        PairingStatus,
        RemoteInstance,
    )

    from socialhome.app_keys import federation_repo_key

    repo = client.app[federation_repo_key]
    await repo.save_instance(
        RemoteInstance(
            id="p" * 32,
            display_name="Auntie Mabel's House",
            remote_identity_pk="ab" * 32,
            key_self_to_remote="00",
            key_remote_to_self="00",
            remote_inbox_url="https://peer.example/federation/inbox/SECRETINBOXID",
            local_inbox_id="wh-local",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        ),
    )

    resp = await client.get("/api/admin/diagnostics", headers=_auth(client._tok))
    raw = await resp.text()

    assert "SECRETINBOXID" not in raw, "per-pair inbox secret leaked"
    assert "Auntie Mabel" not in raw, "household name leaked"
    # The useful half survives.
    body = await resp.json()
    peer = next(p for p in body["peers"] if p["instance_id"] == "p" * 32)
    assert peer["inbox_host"] == "https://peer.example"
    assert peer["status"] == "confirmed"
    # The reachability fields are the reason this section exists.
    assert "last_reachable_at" in peer
    assert "unreachable_since" in peer


async def test_diagnostics_redacts_turn_credentials(client):
    from socialhome.app_keys import federation_service_key

    client.app[federation_service_key].set_ice_servers(
        [
            {
                "urls": ["turn:t.example:3478"],
                "username": "1780000000:iid",
                "credential": "SUPER-SECRET-HMAC",
            },
        ],
    )
    resp = await client.get("/api/admin/diagnostics", headers=_auth(client._tok))
    raw = await resp.text()
    assert "SUPER-SECRET-HMAC" not in raw
    assert "1780000000:iid" not in raw
    body = await resp.json()
    assert body["webrtc"]["turn_usable"] is True


async def test_diagnostics_reports_the_outbox_backlog(client):
    """A stuck backlog is invisible in the UI and was the entire story in
    the production log this bundle was designed from."""
    db = client._db
    await db.enqueue(
        "INSERT INTO federation_outbox(id, instance_id, event_type,"
        " payload_json, status, attempts, next_attempt_at, created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (
            "e1",
            "q" * 32,
            "space_post_created",
            "{}",
            "pending",
            9,
            "2026-09-11T00:00:00+00:00",
            "2026-09-10T00:00:00+00:00",
        ),
    )
    resp = await client.get("/api/admin/diagnostics", headers=_auth(client._tok))
    body = await resp.json()
    row = next(r for r in body["outbox"] if r["instance_id"] == "q" * 32)
    assert row["count"] == 1
    assert row["max_attempts"] == 9
    assert row["status"] == "pending"


async def test_diagnostics_download_sets_a_filename(client):
    resp = await client.get(
        "/api/admin/diagnostics?download=1", headers=_auth(client._tok)
    )
    assert resp.status == 200
    cd = resp.headers.get("Content-Disposition", "")
    assert "attachment" in cd
    assert "socialhome-diagnostics-" in cd
    assert cd.endswith('.json"')


async def test_diagnostics_plain_get_has_no_attachment_header(client):
    resp = await client.get("/api/admin/diagnostics", headers=_auth(client._tok))
    assert "Content-Disposition" not in resp.headers
