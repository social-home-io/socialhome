"""HTTP tests for ``GET /api/peer_spaces``."""

from __future__ import annotations

from .conftest import _auth


async def _peer(client, iid: str, display_name: str, status: str = "confirmed") -> None:
    await client._db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status) VALUES(?, ?, 'pk', 'k1', 'k2', '', ?, ?)",
        (iid, display_name, f"inbox-{iid}", status),
    )


async def _listed(client, iid: str, space_id: str) -> None:
    await client._db.enqueue(
        "INSERT INTO peer_space_directory(instance_id, space_id, name)"
        " VALUES(?, ?, 'A space')",
        (iid, space_id),
    )


async def test_rows_carry_the_paired_host_name_and_never_its_id(client):
    """A paired household's name, from the pairing; an unnamed one is
    ``null`` — never its instance id standing in for a name."""
    await _peer(client, "hh-keller", "Keller household")
    await _peer(client, "hh-blank", "")
    await _listed(client, "hh-keller", "sp-k")
    await _listed(client, "hh-blank", "sp-b")
    r = await client.get("/api/peer_spaces", headers=_auth(client._tok))
    assert r.status == 200
    rows = {row["space_id"]: row for row in await r.json()}
    assert rows["sp-k"]["host_display_name"] == "Keller household"
    assert rows["sp-k"]["host_is_paired"] is True
    assert rows["sp-b"]["host_display_name"] is None
    assert rows["sp-b"]["host_instance_id"] == "hh-blank"


async def test_rows_from_an_unpaired_or_unknown_host_are_not_listed(client):
    await _peer(client, "hh-gone", "Gone household", status="pending_sent")
    await _listed(client, "hh-gone", "sp-gone")
    await _listed(client, "hh-unknown", "sp-unknown")
    r = await client.get("/api/peer_spaces", headers=_auth(client._tok))
    assert r.status == 200
    assert await r.json() == []
