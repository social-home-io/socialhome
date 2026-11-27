"""``space.viewing`` (v_97) — a person has a space open, so the household
watches its GFS channel ring. Only a member's space counts."""

from __future__ import annotations

import asyncio
import json

from socialhome.services.channel_ring_client import ChannelRingClient


async def _space(client, space_id: str, *, member: bool) -> None:
    await client._db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username, "
        "identity_public_key) VALUES(?, 'S', 'inst-x', 'admin', ?)",
        (space_id, "aa" * 32),
    )
    if member:
        await client._db.enqueue(
            "INSERT INTO space_members(space_id, user_id, role) VALUES(?, ?, 'member')",
            (space_id, client._uid),
        )


async def test_a_member_viewing_a_space_reaches_the_ring_client(client, monkeypatch):
    seen: list[str] = []

    async def viewing(self, space_id: str) -> None:
        seen.append(space_id)

    monkeypatch.setattr(ChannelRingClient, "viewing", viewing)
    await _space(client, "sp-mine", member=True)
    await _space(client, "sp-other", member=False)
    ws = await client.ws_connect(f"/api/ws?token={client._tok}")
    try:
        for body in (
            {"type": "space.viewing", "data": {"space_id": "sp-other"}},
            {"type": "space.viewing", "data": {"space_id": 7}},
            {"type": "space.viewing", "data": "x"},
            {"type": "space.viewing", "data": {"space_id": "sp-mine"}},
        ):
            await ws.send_str(json.dumps(body))
        for _ in range(40):
            if seen:
                break
            await asyncio.sleep(0.05)
        # The socket survived every frame.
        await ws.send_str("ping")
        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
        assert msg.data == "pong"
    finally:
        await ws.close()
    assert seen == ["sp-mine"]
