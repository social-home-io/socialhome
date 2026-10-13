"""Route tests for /api/friends — the connected-people dashboard.

Coverage:
* Empty: caller with no paired peers gets the local block + an empty
  households list.
* Populated: a confirmed remote instance with members shows up; a
  pending one does not.
* Privacy: sensitive fields (routing keys, inbox URLs, identity PKs)
  never appear in the JSON response.
* Null coords: a remote_instance with NULL home_lat/home_lon doesn't
  crash and exposes ``None`` in the payload.
"""

from __future__ import annotations

from socialhome.app_keys import db_key, federation_repo_key
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)

from .conftest import _auth


def _peer(
    iid: str,
    *,
    status: PairingStatus = PairingStatus.CONFIRMED,
    home_lat: float | None = None,
    home_lon: float | None = None,
) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=f"{iid.title()} Household",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url=f"https://{iid}.example/wh",
        local_inbox_id=f"wh-{iid}",
        status=status,
        source=InstanceSource.MANUAL,
        home_lat=home_lat,
        home_lon=home_lon,
    )


async def _seed_remote_user(
    db,
    *,
    user_id: str,
    instance_id: str,
    name: str,
) -> None:
    await db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username, "
        " display_name) VALUES(?,?,?,?)",
        (user_id, instance_id, name.lower(), name),
    )


# ─── Smoke ───────────────────────────────────────────────────────────────


async def test_friends_returns_local_block_when_no_pairs(client):
    r = await client.get("/api/friends", headers=_auth(client._tok))
    assert r.status == 200
    body = await r.json()
    assert "instance" in body
    assert body["households"] == []
    # Local block carries our admin user.
    member_ids = {m["user_id"] for m in body["instance"]["members"]}
    assert client._uid in member_ids
    # Totals: 1 household (us), N people (≥1).
    assert body["totals"]["households"] == 1
    assert body["totals"]["people"] >= 1


# ─── Population ──────────────────────────────────────────────────────────


async def test_friends_lists_confirmed_remote_household(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_peer("peer-a", home_lat=52.3676, home_lon=4.9041))
    await _seed_remote_user(
        client._db,
        user_id="ru-1",
        instance_id="peer-a",
        name="Bob",
    )
    await _seed_remote_user(
        client._db,
        user_id="ru-2",
        instance_id="peer-a",
        name="Carol",
    )
    r = await client.get("/api/friends", headers=_auth(client._tok))
    body = await r.json()
    assert len(body["households"]) == 1
    h = body["households"][0]
    assert h["instance_id"] == "peer-a"
    assert h["display_name"] == "Peer-A Household"
    assert h["home_lat"] == 52.3676
    assert h["home_lon"] == 4.9041
    assert h["member_count"] == 2
    assert {m["user_id"] for m in h["members"]} == {"ru-1", "ru-2"}


async def test_friends_carries_presence_triple_for_remote_members(client):
    """Regression: a remote-household member used to render on the
    Friends page without an online indicator because
    :func:`_remote_user_to_dict` skipped the presence triple.
    Local rows already carry ``is_online`` / ``is_idle`` /
    ``last_seen_at`` so the SPA can draw the avatar dot + OnlinePill;
    remote rows need the same shape so the indicator appears for
    federated peers too. Offline by default in a fresh test (no
    ``USER_ONLINE`` envelope landed).
    """
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_peer("peer-a"))
    await _seed_remote_user(
        client._db,
        user_id="ru-1",
        instance_id="peer-a",
        name="Bob",
    )
    r = await client.get("/api/friends", headers=_auth(client._tok))
    body = await r.json()
    h = body["households"][0]
    m = next(x for x in h["members"] if x["user_id"] == "ru-1")
    # The presence triple is part of the contract — same field names,
    # same shape as the local block.
    assert m["is_online"] is False
    assert m["is_idle"] is False
    assert "last_seen_at" in m
    assert m["last_seen_at"] is None


async def test_friends_omits_pending_pair(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(
        _peer("peer-pending", status=PairingStatus.PENDING_RECEIVED),
    )
    r = await client.get("/api/friends", headers=_auth(client._tok))
    body = await r.json()
    assert body["households"] == []


async def test_friends_handles_null_coordinates(client):
    fed_repo = client.app[federation_repo_key]
    # No coords — the column is nullable; the route must tolerate it.
    await fed_repo.save_instance(_peer("peer-blind"))
    r = await client.get("/api/friends", headers=_auth(client._tok))
    body = await r.json()
    h = body["households"][0]
    assert h["home_lat"] is None
    assert h["home_lon"] is None


async def test_friends_payload_omits_sensitive_fields(client):
    """Privacy guard — routing keys, inbox URLs, and identity PKs must
    never appear in the JSON. Verifies the whitelist-only serialiser
    in :func:`_instance_safe_dict`."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_peer("peer-secret"))
    r = await client.get("/api/friends", headers=_auth(client._tok))
    raw = await r.text()
    for forbidden in (
        "routing_secret",
        "remote_inbox_url",
        "key_self_to_remote",
        "key_remote_to_self",
        "local_inbox_id",
        "remote_identity_pk",
        "remote_pq_identity_pk",
    ):
        assert forbidden not in raw, f"{forbidden!r} leaked in /api/friends response"


async def test_friends_totals_include_local_and_remote(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_peer("peer-x"))
    await _seed_remote_user(
        client._db,
        user_id="ru-x1",
        instance_id="peer-x",
        name="Dani",
    )
    await fed_repo.save_instance(_peer("peer-y"))
    await _seed_remote_user(
        client._db,
        user_id="ru-y1",
        instance_id="peer-y",
        name="Erin",
    )
    await _seed_remote_user(
        client._db,
        user_id="ru-y2",
        instance_id="peer-y",
        name="Frida",
    )
    r = await client.get("/api/friends", headers=_auth(client._tok))
    body = await r.json()
    assert body["totals"]["households"] == 3  # us + peer-x + peer-y
    assert body["totals"]["people"] >= 1 + 1 + 2  # admin + Dani + Erin/Frida


async def test_friends_local_instance_carries_identity_metadata(client):
    """The local block must carry instance_id + display_name read from
    the ``instance_identity`` row populated at boot (not None / empty
    placeholders)."""
    db = client.app[db_key]
    # Patch the row so we can assert it round-trips.
    await db.enqueue(
        "UPDATE instance_identity SET display_name=?, home_lat=?, home_lon=?"
        " WHERE id='self'",
        ("Vizeli Home", 52.0907, 5.1214),
    )
    r = await client.get("/api/friends", headers=_auth(client._tok))
    body = await r.json()
    inst = body["instance"]
    assert inst["display_name"] == "Vizeli Home"
    assert inst["home_lat"] == 52.0907
    assert inst["home_lon"] == 5.1214
    assert inst["instance_id"]  # non-empty string


async def test_friends_signs_picture_url_for_remote_members(client):
    """Avatars on the Friends grid render via plain ``<img src>``, so
    each ``picture_url`` returned must carry a signed ``?exp=&sig=``
    query — the canonical ``/api/users/{id}/picture`` URL would 401
    because the browser drops the Authorization header on media-element
    requests."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_peer("peer-pic"))
    await client._db.enqueue(
        "INSERT INTO remote_users("
        "  user_id, instance_id, remote_username, display_name, picture_hash"
        ") VALUES(?,?,?,?,?)",
        ("ru-with-pic", "peer-pic", "gus", "Gus", "deadbeefdeadbeef"),
    )
    r = await client.get("/api/friends", headers=_auth(client._tok))
    body = await r.json()
    h = next(h for h in body["households"] if h["instance_id"] == "peer-pic")
    member = next(m for m in h["members"] if m["user_id"] == "ru-with-pic")
    pic = member["picture_url"]
    # Relative path (no leading ``/``) so the SPA's ``<img src>``
    # resolves against ``<base href>`` and stays ingress-safe.
    assert pic.startswith("api/users/ru-with-pic/picture?"), pic
    assert "exp=" in pic
    assert "sig=" in pic


async def test_friends_drops_blocked_local_and_remote_users(client):
    """A blocked household member is filtered from the local block; a
    blocked remote user is filtered from their household's member list."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_peer("peer-block"))
    await _seed_remote_user(
        client._db,
        user_id="ru-blocked",
        instance_id="peer-block",
        name="Blocky",
    )
    await _seed_remote_user(
        client._db,
        user_id="ru-visible",
        instance_id="peer-block",
        name="Visible",
    )
    # Add a second local user so we can verify the local-block filter too.
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("local_blockee", "uid-local-blockee", "LocalBlockee"),
    )
    # Block both via the API, then assert the dashboard hides them.
    for uid in ("ru-blocked", "uid-local-blockee"):
        r = await client.post(
            "/api/blocks",
            json={"user_id": uid},
            headers=_auth(client._tok),
        )
        assert r.status == 201

    r = await client.get("/api/friends", headers=_auth(client._tok))
    body = await r.json()
    local_ids = {m["user_id"] for m in body["instance"]["members"]}
    assert "uid-local-blockee" not in local_ids
    h = next(h for h in body["households"] if h["instance_id"] == "peer-block")
    remote_ids = {m["user_id"] for m in h["members"]}
    assert "ru-blocked" not in remote_ids
    assert "ru-visible" in remote_ids
    assert h["member_count"] == 1


# ─── Personal alias surfacing ────────────────────────────────────────────


async def test_friends_returns_personal_alias_per_remote_member(client):
    """Sets a personal alias for a remote peer's member via the
    alias API, then asserts ``/api/friends`` carries it on that
    member's row so the SPA can render it in place of display_name
    + show the "your nickname" ✏ indicator."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_peer("peer-x", home_lat=52.5, home_lon=13.4))
    await _seed_remote_user(
        client._db,
        user_id="ru-bob",
        instance_id="peer-x",
        name="Bob",
    )

    # Set alias via the existing endpoint.
    put = await client.put(
        "/api/aliases/users/ru-bob",
        json={"alias": "Brother"},
        headers=_auth(client._tok),
    )
    assert put.status in (200, 201), await put.text()

    r = await client.get("/api/friends", headers=_auth(client._tok))
    body = await r.json()
    h = next(h for h in body["households"] if h["instance_id"] == "peer-x")
    bob_row = next(m for m in h["members"] if m["user_id"] == "ru-bob")
    assert bob_row["personal_alias"] == "Brother"
    # Original display_name still travels — the SPA shows the alias
    # but falls back to display_name when the user clears it.
    assert bob_row["display_name"] == "Bob"


async def test_friends_personal_alias_does_not_leak_across_viewers(client):
    """Aliases are viewer-private: a row in ``user_aliases`` set by a
    DIFFERENT viewer must not surface on the caller's
    ``/api/friends`` payload. We seed a second local user and insert
    that user's alias row directly so we don't need a second login.
    """
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_peer("peer-y", home_lat=53, home_lon=14))
    await _seed_remote_user(
        client._db,
        user_id="ru-c",
        instance_id="peer-y",
        name="Carol",
    )
    # Seed a second local user so the FK on user_aliases.viewer_user_id
    # is satisfied; pretend she aliased Carol.
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("eve", "uid-eve", "Eve"),
    )
    await client._db.enqueue(
        "INSERT INTO user_aliases(viewer_user_id, target_user_id, alias) "
        "VALUES(?, ?, ?)",
        ("uid-eve", "ru-c", "NotForYou"),
    )
    # The default caller hasn't aliased Carol — alias must come back null.
    r = await client.get("/api/friends", headers=_auth(client._tok))
    body = await r.json()
    h = next(h for h in body["households"] if h["instance_id"] == "peer-y")
    carol = next(m for m in h["members"] if m["user_id"] == "ru-c")
    assert carol["personal_alias"] is None, (
        "alias leaked across viewers — resolver should key on viewer_user_id"
    )
