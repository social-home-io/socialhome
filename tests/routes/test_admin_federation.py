"""Tests for ``GET /api/admin/federation/compat`` (admin federation panel).

Admin-only list of confirmed peers with their advertised proto_version,
the features they lack vs OURS, and whether they've ever advertised
capabilities (NULL ``capabilities_seen_at`` ⇒ never).
"""

from __future__ import annotations

from socialhome.domain.federation_capabilities import OURS, features_missing_below

from .conftest import _auth


async def _seed_peer(
    db,
    *,
    instance_id: str,
    display_name: str,
    proto_version: int,
    status: str = "confirmed",
    capabilities_seen_at: str | None = None,
    last_reachable_at: str | None = None,
) -> None:
    await db.enqueue(
        """
        INSERT INTO remote_instances(
            id, display_name, remote_identity_pk,
            key_self_to_remote, key_remote_to_self,
            remote_inbox_url, local_inbox_id, status, source,
            proto_version, capabilities_seen_at, last_reachable_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            instance_id,
            display_name,
            "ab" * 32,
            "00",
            "00",
            f"https://{instance_id}.example/inbox/x",
            instance_id + "_local",
            status,
            "manual",
            proto_version,
            capabilities_seen_at,
            last_reachable_at,
        ),
    )


async def test_compat_requires_admin(client):
    """A non-admin token gets 403."""
    db = client._db
    # Demote the seeded admin user.
    await db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    resp = await client.get("/api/admin/federation/compat", headers=_auth(client._tok))
    assert resp.status == 403


async def test_compat_lists_peer_below_ours(client):
    """A confirmed peer below OURS reports non-empty lacking_features and
    capabilities_known reflecting its capabilities_seen_at stamp."""
    db = client._db
    await _seed_peer(
        db,
        instance_id="peer-old",
        display_name="Old House",
        proto_version=13,
        capabilities_seen_at="2026-06-01T00:00:00+00:00",
        last_reachable_at="2026-06-02 10:00:00",
    )
    resp = await client.get("/api/admin/federation/compat", headers=_auth(client._tok))
    assert resp.status == 200
    body = await resp.json()
    assert body["ours"] == OURS
    peers = {p["instance_id"]: p for p in body["peers"]}
    p = peers["peer-old"]
    assert p["display_name"] == "Old House"
    assert p["proto_version"] == 13
    assert p["status"] == "confirmed"
    assert p["last_reachable_at"] == "2026-06-02 10:00:00"
    assert p["capabilities_known"] is True
    assert p["lacking_features"] == features_missing_below(13)
    assert p["lacking_features"]  # non-empty


async def test_compat_peer_at_ours_lacks_nothing(client):
    """A confirmed peer at OURS reports an empty lacking_features list."""
    db = client._db
    await _seed_peer(
        db,
        instance_id="peer-current",
        display_name="Current House",
        proto_version=OURS,
        capabilities_seen_at="2026-06-03T00:00:00+00:00",
    )
    resp = await client.get("/api/admin/federation/compat", headers=_auth(client._tok))
    body = await resp.json()
    peers = {p["instance_id"]: p for p in body["peers"]}
    assert peers["peer-current"]["lacking_features"] == []


async def test_compat_capabilities_known_distinguishes_never_advertised(client):
    """A NULL capabilities_seen_at ⇒ capabilities_known false (never
    advertised) vs a stamped peer ⇒ true."""
    db = client._db
    await _seed_peer(
        db,
        instance_id="peer-fresh",
        display_name="Fresh Pair",
        proto_version=1,
        capabilities_seen_at=None,
    )
    await _seed_peer(
        db,
        instance_id="peer-seen",
        display_name="Seen Pair",
        proto_version=1,
        capabilities_seen_at="2026-06-01T00:00:00+00:00",
    )
    resp = await client.get("/api/admin/federation/compat", headers=_auth(client._tok))
    body = await resp.json()
    peers = {p["instance_id"]: p for p in body["peers"]}
    assert peers["peer-fresh"]["capabilities_known"] is False
    assert peers["peer-seen"]["capabilities_known"] is True


async def test_compat_excludes_pending_peers(client):
    """Only confirmed peers appear — a pending pair is not listed."""
    db = client._db
    await _seed_peer(
        db,
        instance_id="peer-pending",
        display_name="Pending",
        proto_version=1,
        status="pending_sent",
    )
    resp = await client.get("/api/admin/federation/compat", headers=_auth(client._tok))
    body = await resp.json()
    ids = {p["instance_id"] for p in body["peers"]}
    assert "peer-pending" not in ids


# ── POST /api/admin/federation/resync ───────────────────────────────


async def test_resync_requires_admin(client):
    """A non-admin token gets 403."""
    db = client._db
    await db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "peer-x", "scope": "capabilities"},
    )
    assert resp.status == 403


async def test_resync_rejects_bad_scope(client):
    """An unrecognised scope is 400 before any peer lookup."""
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "peer-x", "scope": "nonsense"},
    )
    assert resp.status == 400


async def test_resync_rejects_empty_instance(client):
    """A missing instance_id is 400."""
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "", "scope": "capabilities"},
    )
    assert resp.status == 400


async def test_resync_rejects_space_scope_without_id(client):
    """``space:`` with an empty id is 400."""
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "peer-x", "scope": "space:"},
    )
    assert resp.status == 400


async def test_resync_peer_too_old_is_409(client):
    """A confirmed peer below v_19 can't honour the request → 409."""
    db = client._db
    await _seed_peer(
        db,
        instance_id="peer-old",
        display_name="Old House",
        proto_version=13,
        capabilities_seen_at="2026-06-01T00:00:00+00:00",
    )
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "peer-old", "scope": "capabilities"},
    )
    assert resp.status == 409


async def test_resync_unknown_peer_is_409(client):
    """An unknown peer (peer_supports False) → 409."""
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "no-such-peer", "scope": "capabilities"},
    )
    assert resp.status == 409


async def test_resync_v19_peer_capabilities_is_200(client):
    """A confirmed v_19 peer accepts the resync request → 200."""
    db = client._db
    await _seed_peer(
        db,
        instance_id="peer-new",
        display_name="New House",
        proto_version=OURS,
        capabilities_seen_at="2026-06-04T00:00:00+00:00",
    )
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "peer-new", "scope": "capabilities"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {
        "status": "ok",
        "instance_id": "peer-new",
        "scope": "capabilities",
    }


async def test_resync_v19_peer_space_scope_is_200(client):
    """A ``space:<id>`` scope against a v_19 peer → 200."""
    db = client._db
    await _seed_peer(
        db,
        instance_id="peer-new2",
        display_name="New House 2",
        proto_version=OURS,
        capabilities_seen_at="2026-06-04T00:00:00+00:00",
    )
    resp = await client.post(
        "/api/admin/federation/resync",
        headers=_auth(client._tok),
        json={"instance_id": "peer-new2", "scope": "space:sp-1"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["scope"] == "space:sp-1"


# ── GET / PUT /api/admin/federation/external-url ──────────────────────
#
# The admin-set federation inbox base URL. Before this endpoint the only
# sources were socialhome.toml (operator-owned, not writable from the UI)
# and the Home Assistant integration — while the pairing error told
# admins to "set this Social Home's external URL in Settings", a field
# that existed nowhere in the SPA.


async def test_external_url_requires_admin(client):
    db = client._db
    await db.enqueue("UPDATE users SET is_admin=0 WHERE user_id=?", (client._uid,))
    for call in (
        client.get("/api/admin/federation/external-url", headers=_auth(client._tok)),
        client.put(
            "/api/admin/federation/external-url",
            json={"base": "https://h.example"},
            headers=_auth(client._tok),
        ),
    ):
        resp = await call
        assert resp.status == 403


async def test_external_url_unset_reads_null(client):
    resp = await client.get(
        "/api/admin/federation/external-url", headers=_auth(client._tok)
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["base"] is None


async def test_external_url_round_trips(client):
    resp = await client.put(
        "/api/admin/federation/external-url",
        json={"base": "https://home.example.com"},
        headers=_auth(client._tok),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["base"] == "https://home.example.com"
    assert body["changed"] is True

    resp = await client.get(
        "/api/admin/federation/external-url", headers=_auth(client._tok)
    )
    body = await resp.json()
    assert body["base"] == "https://home.example.com"
    assert body["source"] == "manual"
    # The adapter resolves it with Social Home's own inbox path appended —
    # that is what a peer POSTs to.
    assert body["effective"] == "https://home.example.com/federation/inbox"


async def test_external_url_strips_trailing_slash_and_inbox_path(client):
    """Pasting the full inbox URL is the obvious mistake; don't double it."""
    resp = await client.put(
        "/api/admin/federation/external-url",
        json={"base": "https://home.example.com/federation/inbox/"},
        headers=_auth(client._tok),
    )
    assert resp.status == 200
    assert (await resp.json())["base"] == "https://home.example.com"

    resp = await client.get(
        "/api/admin/federation/external-url", headers=_auth(client._tok)
    )
    assert (await resp.json())["effective"] == (
        "https://home.example.com/federation/inbox"
    )


async def test_external_url_rejects_non_http(client):
    for bad in ("home.example.com", "ftp://h.example", "javascript:alert(1)", "   "):
        resp = await client.put(
            "/api/admin/federation/external-url",
            json={"base": bad},
            headers=_auth(client._tok),
        )
        assert resp.status in (200, 422), bad
        if bad.strip():
            assert resp.status == 422, bad


async def test_external_url_rejects_what_peers_would_refuse(client):
    """The admin's own base gets the same household-address rules peers
    apply to it — storing one they'd refuse would silently break pairing."""
    for bad in (
        "https://user:pw@home.example.com",
        "https://home.example.com/in box",
        "https://:8123",
    ):
        resp = await client.put(
            "/api/admin/federation/external-url",
            json={"base": bad},
            headers=_auth(client._tok),
        )
        assert resp.status == 422, bad
    # A LAN name over plain http is still a valid household base.
    resp = await client.put(
        "/api/admin/federation/external-url",
        json={"base": "http://homeassistant.local:8123"},
        headers=_auth(client._tok),
    )
    assert resp.status == 200


async def test_external_url_clears_back_to_the_automatic_source(client):
    """An empty value hands control back, rather than storing a blank."""
    await client.put(
        "/api/admin/federation/external-url",
        json={"base": "https://home.example.com"},
        headers=_auth(client._tok),
    )
    resp = await client.put(
        "/api/admin/federation/external-url",
        json={"base": None},
        headers=_auth(client._tok),
    )
    assert resp.status == 200
    assert (await resp.json())["base"] is None

    resp = await client.get(
        "/api/admin/federation/external-url", headers=_auth(client._tok)
    )
    body = await resp.json()
    assert body["base"] is None
    # No row left behind — a blank string would make the adapter's
    # "is it set?" check true while resolving to nothing useful.
    rows = await client._db.fetchall(
        "SELECT value FROM instance_config WHERE key='federation_base_url'",
    )
    assert rows == []


async def test_external_url_unchanged_value_reports_not_changed(client):
    """Re-submitting the same value must not re-notify peers."""
    for _ in range(2):
        resp = await client.put(
            "/api/admin/federation/external-url",
            json={"base": "https://home.example.com"},
            headers=_auth(client._tok),
        )
        assert resp.status == 200
        body = await resp.json()
    assert body["changed"] is False
    assert body["peers_notified"] == 0


# ── GET /api/admin/federation/ice-servers ─────────────────────────────
#
# Read-only diagnostic overview. Whether RTC can traverse a network is
# the most opaque thing about a federation deployment: a missing or
# credential-less TURN entry degrades silently to HTTPS, with the only
# evidence a log warning nobody reads.


async def test_ice_servers_requires_admin(client):
    db = client._db
    await db.enqueue("UPDATE users SET is_admin=0 WHERE user_id=?", (client._uid,))
    resp = await client.get(
        "/api/admin/federation/ice-servers", headers=_auth(client._tok)
    )
    assert resp.status == 403


async def test_ice_servers_never_leaks_the_credential(client):
    """The whole point of the redaction: ``credential`` is an HMAC of the
    operator's shared secret, so exposing it hands out relay access."""
    from socialhome.app_keys import federation_service_key

    fed = client.app[federation_service_key]
    fed.set_ice_servers(
        [
            {"urls": ["stun:stun.example:3478"]},
            {
                "urls": ["turn:t.example:3478", "turns:t.example:5349"],
                "username": "1780000000:iid",
                "credential": "SUPER-SECRET-HMAC",
            },
        ],
    )

    resp = await client.get(
        "/api/admin/federation/ice-servers", headers=_auth(client._tok)
    )
    assert resp.status == 200
    body = await resp.json()
    # Serialise the whole response and scan it, so a secret leaking via
    # any field (not just the one we thought of) still fails the test.
    raw = str(body)

    assert "SUPER-SECRET-HMAC" not in raw
    assert "1780000000:iid" not in raw
    # The useful facts survive.
    assert body["servers"][1]["urls"] == [
        "turn:t.example:3478",
        "turns:t.example:5349",
    ]
    assert body["servers"][1]["has_credentials"] is True
    assert body["servers"][0]["has_credentials"] is False
    assert body["has_turn"] is True
    assert body["turn_usable"] is True


async def test_ice_servers_flags_a_credential_less_turn(client):
    """`turn_usable` must be false for TURN with no credentials — the
    case that silently falls back to HTTPS."""
    from socialhome.app_keys import federation_service_key

    client.app[federation_service_key].set_ice_servers(
        [{"urls": ["turn:t.example:3478"]}],
    )
    resp = await client.get(
        "/api/admin/federation/ice-servers", headers=_auth(client._tok)
    )
    body = await resp.json()
    assert body["has_turn"] is True
    assert body["turn_usable"] is False


async def test_ice_servers_reports_stun_only(client):
    from socialhome.app_keys import federation_service_key

    client.app[federation_service_key].set_ice_servers(
        [{"urls": ["stun:stun.example:3478"]}],
    )
    resp = await client.get(
        "/api/admin/federation/ice-servers", headers=_auth(client._tok)
    )
    body = await resp.json()
    assert body["has_turn"] is False
    assert body["turn_usable"] is False
    assert body["servers"][0]["kinds"] == ["stun"]
    # standalone in tests — nothing replaces the config list here.
    assert body["pulls_from_home_assistant"] is False
