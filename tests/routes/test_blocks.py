"""HTTP tests for /api/blocks/* — personal user blocks (§Privacy)."""

from __future__ import annotations

from socialhome.app_keys import federation_repo_key, user_repo_key
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)

from .conftest import _auth


async def _seed_target(client, *, user_id="uid-bob", username="bob"):
    """Insert a second local user that admin can block."""
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (username, user_id, "Bob"),
    )


async def test_get_blocks_empty(client):
    r = await client.get("/api/blocks", headers=_auth(client._tok))
    assert r.status == 200
    assert (await r.json()) == {"blocks": []}


async def test_post_block_then_get(client):
    await _seed_target(client)
    r = await client.post(
        "/api/blocks",
        json={"user_id": "uid-bob"},
        headers=_auth(client._tok),
    )
    assert r.status == 201
    assert (await r.json()) == {"user_id": "uid-bob"}

    r = await client.get("/api/blocks", headers=_auth(client._tok))
    body = await r.json()
    assert [b["user_id"] for b in body["blocks"]] == ["uid-bob"]
    assert body["blocks"][0]["blocked_at"]


async def test_post_block_self_rejected(client):
    """Self-block raises ValueError → maps to 422 via BaseView._iter."""
    r = await client.post(
        "/api/blocks",
        json={"user_id": client._uid},
        headers=_auth(client._tok),
    )
    assert r.status == 422


async def test_post_block_missing_user_id(client):
    r = await client.post("/api/blocks", json={}, headers=_auth(client._tok))
    assert r.status == 400


async def test_delete_block(client):
    await _seed_target(client)
    await client.post(
        "/api/blocks",
        json={"user_id": "uid-bob"},
        headers=_auth(client._tok),
    )
    r = await client.delete(
        "/api/blocks/uid-bob",
        headers=_auth(client._tok),
    )
    assert r.status == 200
    r = await client.get("/api/blocks", headers=_auth(client._tok))
    assert (await r.json()) == {"blocks": []}


async def test_delete_block_idempotent(client):
    """Deleting a non-existent block is a no-op (200)."""
    r = await client.delete(
        "/api/blocks/uid-ghost",
        headers=_auth(client._tok),
    )
    assert r.status == 200


async def test_blocks_require_auth(client):
    r = await client.get("/api/blocks")
    assert r.status == 401
    r = await client.post("/api/blocks", json={"user_id": "uid-bob"})
    assert r.status == 401
    r = await client.delete("/api/blocks/uid-bob")
    assert r.status == 401


# ─── Who the blocked people are (B1, Friends/Profile review) ────────────


def _peer(
    iid: str,
    *,
    name: str,
    status: PairingStatus = PairingStatus.CONFIRMED,
) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=name,
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url=f"https://{iid}.example/wh",
        local_inbox_id=f"wh-{iid}",
        status=status,
        source=InstanceSource.MANUAL,
    )


async def _seed_remote(client, *, user_id, instance_id, name, picture_hash=None):
    await client._db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username, "
        " display_name, picture_hash) VALUES(?,?,?,?,?)",
        (user_id, instance_id, name.lower(), name, picture_hash),
    )


async def _block(client, user_id):
    r = await client.post(
        "/api/blocks", json={"user_id": user_id}, headers=_auth(client._tok)
    )
    assert r.status == 201, await r.text()


async def _blocks_by_id(client) -> dict[str, dict]:
    r = await client.get("/api/blocks", headers=_auth(client._tok))
    assert r.status == 200
    return {b["user_id"]: b for b in (await r.json())["blocks"]}


async def test_get_blocks_names_a_local_person_with_own_household(client):
    """Regression: the list carried bare ids, so the SPA named people from
    the local roster only. A local person is named after their own
    household."""
    await client.app[federation_repo_key].set_instance_display_name("Vizeli")
    await _seed_target(client)
    await _block(client, "uid-bob")
    row = (await _blocks_by_id(client))["uid-bob"]
    assert row["display_name"] == "Bob"
    assert row["household"] == "Vizeli"
    assert row["personal_alias"] is None
    assert row["picture_url"] is None
    assert row["blocked_at"]


async def test_get_blocks_names_a_remote_person_with_their_household(client):
    """Regression (B1): a remote person rendered as ``ru-7ae1d43cad72``.
    Now their real name, signed picture and household (the admin's local
    alias for it wins) travel with the row."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_peer("peer-r", name="z7k63zfi"))
    await fed_repo.update_alias("peer-r", "Müller-Lüdenscheidt")
    await _seed_remote(
        client,
        user_id="ru-7ae1d43cad72",
        instance_id="peer-r",
        name="Annegret",
        picture_hash="deadbeefdeadbeef",
    )
    await _block(client, "ru-7ae1d43cad72")
    row = (await _blocks_by_id(client))["ru-7ae1d43cad72"]
    assert row["display_name"] == "Annegret"
    assert row["household"] == "Müller-Lüdenscheidt"
    # Signed so a plain <img src> loads without a bearer token.
    assert row["picture_url"].startswith(
        "api/users/ru-7ae1d43cad72/picture?v=deadbeefdeadbeef"
    )
    assert "sig=" in row["picture_url"]


async def test_get_blocks_carries_the_viewers_nickname(client):
    await client.app[federation_repo_key].save_instance(_peer("peer-r", name="Vizeli"))
    await _seed_remote(client, user_id="ru-ruth", instance_id="peer-r", name="Ruth")
    put = await client.put(
        "/api/aliases/users/ru-ruth",
        json={"alias": "Oma"},
        headers=_auth(client._tok),
    )
    assert put.status in (200, 201), await put.text()
    await _block(client, "ru-ruth")
    row = (await _blocks_by_id(client))["ru-ruth"]
    assert row["personal_alias"] == "Oma"
    assert row["display_name"] == "Ruth"


async def test_get_blocks_unknown_person_has_no_name_never_the_raw_id(client):
    """A household that was unpaired and forgotten takes its people with
    it (``remote_users`` cascades); the block stays. The row says
    "nobody we know" (``display_name: null``) and the SPA localises it."""
    await _block(client, "ru-gone")
    row = (await _blocks_by_id(client))["ru-gone"]
    assert row["display_name"] is None
    assert row["household"] is None
    assert row["picture_url"] is None


async def test_get_blocks_person_from_an_unpaired_household(client):
    """Regression (B1): unpairing drops the household's people right away
    (``mark_unpairing``) while the viewer's block stays. The row must not
    fall back to the raw id — it reads as an unknown person."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_peer("peer-u", name="Okafor family"))
    await _seed_remote(client, user_id="ru-amaka", instance_id="peer-u", name="Amaka")
    await _block(client, "ru-amaka")
    await fed_repo.mark_unpairing("peer-u")
    row = (await _blocks_by_id(client))["ru-amaka"]
    assert row["display_name"] is None
    assert row["household"] is None


async def test_get_blocks_known_person_whose_household_is_not_paired(client):
    """A person still on file whose household row is an unpair tombstone
    keeps their name; only the household is unknown."""
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(
        _peer("peer-t", name="Okafor family", status=PairingStatus.UNPAIRING)
    )
    await _seed_remote(client, user_id="ru-obi", instance_id="peer-t", name="Obi")
    await _block(client, "ru-obi")
    row = (await _blocks_by_id(client))["ru-obi"]
    assert row["display_name"] == "Obi"
    assert row["household"] is None


async def test_get_blocks_deprovisioned_remote_person_keeps_their_name(client):
    fed_repo = client.app[federation_repo_key]
    await fed_repo.save_instance(_peer("peer-d", name="Okafor family"))
    await _seed_remote(client, user_id="ru-left", instance_id="peer-d", name="Chidi")
    await client.app[user_repo_key].mark_remote_deprovisioned(
        "ru-left", at="2026-10-01T00:00:00+00:00"
    )
    await _block(client, "ru-left")
    row = (await _blocks_by_id(client))["ru-left"]
    assert row["display_name"] == "Chidi"
    assert row["household"] == "Okafor family"
