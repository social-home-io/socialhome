"""Household bans over the HTTP API (P7b).

``POST /api/spaces/{id}/ban`` names a person (``user_id``) or a household
(``instance_id``); ``GET …/bans`` lists both, each row with its ``kind``;
``DELETE …/household-bans/{instance_id}`` lifts a household ban.
"""

from __future__ import annotations

from socialhome.app_keys import federation_repo_key
from socialhome.auth import sha256_token_hash
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)

from .conftest import _auth


async def _space(client) -> str:
    r = await client.post(
        "/api/spaces", json={"name": "Choir"}, headers=_auth(client._tok)
    )
    assert r.status in (200, 201), await r.text()
    return (await r.json())["id"]


async def _paired(client, iid: str) -> None:
    await client.app[federation_repo_key].save_instance(
        RemoteInstance(
            id=iid,
            display_name="Noisy Household",
            remote_identity_pk="aa" * 32,
            key_self_to_remote="enc",
            key_remote_to_self="enc",
            remote_inbox_url=f"https://{iid}.example/wh",
            local_inbox_id=f"wh-{iid}",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        )
    )


async def test_ban_list_and_unban_a_household(client):
    sid = await _space(client)
    await _paired(client, "peer-noisy")
    r = await client.post(
        f"/api/spaces/{sid}/ban",
        json={"instance_id": "peer-noisy", "reason": "spam"},
        headers=_auth(client._tok),
    )
    assert r.status == 200, await r.text()

    r = await client.get(f"/api/spaces/{sid}/bans", headers=_auth(client._tok))
    assert r.status == 200
    [row] = [b for b in await r.json() if b["kind"] == "household"]
    assert row["instance_id"] == "peer-noisy"
    assert row["household_name"] == "Noisy Household"
    assert row["reason"] == "spam"
    assert "user_id" not in row

    r = await client.delete(
        f"/api/spaces/{sid}/household-bans/peer-noisy", headers=_auth(client._tok)
    )
    assert r.status == 200
    r = await client.get(f"/api/spaces/{sid}/bans", headers=_auth(client._tok))
    assert [b for b in await r.json() if b["kind"] == "household"] == []


async def test_person_ban_rows_carry_their_kind(client):
    sid = await _space(client)
    r = await client.post(
        f"/api/spaces/{sid}/ban",
        json={"user_id": "ru-someone"},
        headers=_auth(client._tok),
    )
    assert r.status == 200, await r.text()
    r = await client.get(f"/api/spaces/{sid}/bans", headers=_auth(client._tok))
    [row] = await r.json()
    assert row["kind"] == "person"
    assert row["user_id"] == "ru-someone"


async def test_a_person_ban_row_names_their_household_and_who_banned(client):
    """Ban UX (P8): a person ban row carries the person's household (from
    the seat they held, tombstones included) so the admin can ban the whole
    household from it, and ``banned_by_owner`` so an admin isn't offered an
    Unban the server would refuse."""
    sid = await _space(client)
    await _paired(client, "peer-noisy")
    await client._db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id,"
        " display_name, role, tombstoned) VALUES(?, 'peer-noisy', 'ru-x', 'X',"
        " 'member', 1)",
        (sid,),
    )
    for uid in ("ru-x", "ru-unknown"):
        r = await client.post(
            f"/api/spaces/{sid}/ban", json={"user_id": uid}, headers=_auth(client._tok)
        )
        assert r.status == 200, await r.text()
    r = await client.get(f"/api/spaces/{sid}/bans", headers=_auth(client._tok))
    rows = {b["user_id"]: b for b in await r.json()}
    assert rows["ru-x"]["instance_id"] == "peer-noisy"
    assert rows["ru-x"]["household_name"] == "Noisy Household"
    assert rows["ru-unknown"]["instance_id"] is None
    # The caller is the owner here: both are the owner's bans.
    assert all(b["banned_by_owner"] is True for b in rows.values())


async def test_an_unseated_household_can_be_banned(client):
    """Owner fork B (2026-11-26): a household with no seat (it asked to join,
    or its person already left) can be banned."""
    sid = await _space(client)
    await _paired(client, "peer-asking")
    r = await client.post(
        f"/api/spaces/{sid}/ban",
        json={"instance_id": "peer-asking", "reason": "spam requests"},
        headers=_auth(client._tok),
    )
    assert r.status == 200, await r.text()
    r = await client.get(f"/api/spaces/{sid}/bans", headers=_auth(client._tok))
    [row] = await r.json()
    assert row["banned_by_owner"] is True


async def test_an_unknown_household_lists_without_a_name(client):
    sid = await _space(client)
    r = await client.post(
        f"/api/spaces/{sid}/ban",
        json={"instance_id": "never-paired"},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    r = await client.get(f"/api/spaces/{sid}/bans", headers=_auth(client._tok))
    [row] = await r.json()
    assert row["household_name"] is None


async def test_a_ban_names_exactly_one_target(client):
    sid = await _space(client)
    for body in ({}, {"user_id": "u", "instance_id": "i"}, {"instance_id": 7}):
        r = await client.post(
            f"/api/spaces/{sid}/ban", json=body, headers=_auth(client._tok)
        )
        assert r.status == 422, body


async def test_banning_our_own_household_is_refused(client):
    sid = await _space(client)
    row = await client._db.fetchone("SELECT instance_id FROM instance_identity")
    own = row[0]
    r = await client.post(
        f"/api/spaces/{sid}/ban",
        json={"instance_id": own},
        headers=_auth(client._tok),
    )
    assert r.status == 403
    # A stable code the SPA translates (ban UX review), not raw English.
    err = (await r.json())["error"]
    assert err["code"] == "BAN_NOT_ALLOWED"
    assert err["params"] == {"target": "household"}


async def test_household_bans_are_admin_only(client):
    sid = await _space(client)
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        ("bob", "uid-bob", "Bob"),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-bob", "uid-bob", "t", sha256_token_hash("bob-tok")),
    )
    r = await client.post(
        f"/api/spaces/{sid}/ban",
        json={"instance_id": "peer-noisy"},
        headers=_auth("bob-tok"),
    )
    assert r.status == 403
    r = await client.delete(
        f"/api/spaces/{sid}/household-bans/peer-noisy", headers=_auth("bob-tok")
    )
    assert r.status == 403
