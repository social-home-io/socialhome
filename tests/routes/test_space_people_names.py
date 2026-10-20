"""People in the space admin lists carry a resolved name (#1073 follow-up).

The SPA never shows a raw id: a person it cannot name reads "Someone". The
lists below used to carry only a ``user_id``, so every remote person on them
— banned, asking to join, minting a link, reporting — read "Someone". Each
now carries the name this household holds (per-space name → household /
``remote_users`` name → the space's roster seat → ``None``) and, where the
list shows an avatar, a signed ``picture_url`` — withheld across a guardian
block (§CP.F2), like everywhere else a person is listed.

The bans list is also what the space-sync exporter ships to member
households: the names are added in the route only, and the exporter's rows
stay byte-identical.
"""

from __future__ import annotations

import json

from socialhome.app_keys import federation_repo_key, space_repo_key
from socialhome.auth import sha256_token_hash
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.sync.space.exporters.bans import BansExporter

from .conftest import _auth


def _peer(iid: str) -> RemoteInstance:
    return RemoteInstance(
        id=iid,
        display_name=f"{iid.title()} Household",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url=f"https://{iid}.example/wh",
        local_inbox_id=f"wh-{iid}",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )


async def _space(client, name: str = "Choir") -> str:
    r = await client.post(
        "/api/spaces", json={"name": name}, headers=_auth(client._tok)
    )
    assert r.status in (200, 201), await r.text()
    return (await r.json())["id"]


async def _remote_user(
    client,
    user_id: str,
    name: str,
    *,
    instance_id: str = "peer-v",
    picture_hash: str | None = None,
) -> None:
    fed_repo = client.app[federation_repo_key]
    if await fed_repo.get_instance(instance_id) is None:
        await fed_repo.save_instance(_peer(instance_id))
    await client._db.enqueue(
        "INSERT INTO remote_users(user_id, instance_id, remote_username,"
        " display_name, picture_hash) VALUES(?,?,?,?,?)",
        (user_id, instance_id, name.lower(), name, picture_hash),
    )


async def _seat(client, sid: str, user_id: str, name: str, *, gone: bool) -> None:
    """A roster seat of a household this one never paired with."""
    await client._db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role,"
        " display_name, tombstoned) VALUES(?, 'peer-x', ?, 'member', ?, ?)",
        (sid, user_id, name, 1 if gone else 0),
    )


async def _local_user(
    client,
    user_id: str,
    username: str,
    *,
    picture_hash: str | None = None,
    protected: bool = False,
) -> None:
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, picture_hash,"
        " child_protection_enabled) VALUES(?,?,?,?,?)",
        (username, user_id, username.title(), picture_hash, 1 if protected else 0),
    )


async def _guardian_block(client, minor: str, blocked: str) -> None:
    await client._db.enqueue(
        "INSERT INTO cp_minor_blocks(minor_user_id, blocked_user_id, blocked_by)"
        " VALUES(?,?,?)",
        (minor, blocked, client._uid),
    )


async def _ban(client, sid: str, user_id: str) -> None:
    await client._db.enqueue(
        "INSERT INTO space_bans(space_id, user_id, banned_by, reason) VALUES(?,?,?,?)",
        (sid, user_id, client._uid, "spam"),
    )


async def _bans(client, sid: str) -> dict[str, dict]:
    r = await client.get(f"/api/spaces/{sid}/bans", headers=_auth(client._tok))
    assert r.status == 200, await r.text()
    return {b["user_id"]: b for b in await r.json()}


# ── GET /api/spaces/{id}/bans ─────────────────────────────────────────


async def test_bans_name_remote_people(client):
    sid = await _space(client)
    await _remote_user(
        client, "ru-rita", "Remote Rita", picture_hash="abcd1234abcd1234"
    )
    await _seat(client, sid, "ru-sam", "Stranger Sam", gone=True)
    for uid in ("ru-rita", "ru-sam", "ru-gone"):
        await _ban(client, sid, uid)

    bans = await _bans(client, sid)
    assert bans["ru-rita"]["display_name"] == "Remote Rita"
    assert bans["ru-rita"]["picture_url"].startswith(
        "api/users/ru-rita/picture?v=abcd1234abcd1234"
    )
    assert "sig=" in bans["ru-rita"]["picture_url"]
    # Banned: their seat is tombstoned, but it still knows the name.
    assert bans["ru-sam"]["display_name"] == "Stranger Sam"
    assert bans["ru-sam"]["picture_url"] is None
    # Nobody knows them: no name — never the id.
    assert bans["ru-gone"]["display_name"] is None
    assert bans["ru-gone"]["reason"] == "spam"


async def test_bans_name_without_a_picture_across_a_guardian_block(client):
    """A protected kid whose guardian blocked the admin: the admin's ban
    list names the kid but shows no picture (§CP.F2)."""
    sid = await _space(client)
    await _local_user(
        client, "uid-kid", "kid", picture_hash="ffff0000ffff0000", protected=True
    )
    await _guardian_block(client, "uid-kid", client._uid)
    await _local_user(client, "uid-bob", "bob", picture_hash="eeee1111eeee1111")
    await _ban(client, sid, "uid-kid")
    await _ban(client, sid, "uid-bob")

    bans = await _bans(client, sid)
    assert bans["uid-kid"]["display_name"] == "Kid"
    assert bans["uid-kid"]["picture_url"] is None
    assert bans["uid-bob"]["display_name"] == "Bob"
    assert bans["uid-bob"]["picture_url"]


async def test_bans_names_never_reach_the_sync_exporter(client):
    """The space-sync exporter reads the same repo rows the route does: its
    output is byte-identical before and after the route adds names."""
    sid = await _space(client)
    await _remote_user(
        client, "ru-rita", "Remote Rita", picture_hash="abcd1234abcd1234"
    )
    await _ban(client, sid, "ru-rita")
    exporter = BansExporter(client.app[space_repo_key])

    before = json.dumps(await exporter.list_records(sid), sort_keys=True)
    assert (await _bans(client, sid))["ru-rita"]["display_name"] == "Remote Rita"
    after = json.dumps(await exporter.list_records(sid), sort_keys=True)

    assert after == before
    records = json.loads(after)
    assert set(records[0]) == {
        "space_id",
        "user_id",
        "identity_pk",
        "banned_by",
        "banned_at",
        "reason",
    }


async def test_bans_still_admin_only(client):
    sid = await _space(client)
    await _local_user(client, "uid-bob", "bob")
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-bob", "uid-bob", "t", sha256_token_hash("bob-tok")),
    )
    r = await client.get(f"/api/spaces/{sid}/bans", headers=_auth("bob-tok"))
    assert r.status == 403
