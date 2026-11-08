"""``POST /api/spaces/{id}/gfs-join-requests/decline`` — an admin declines
every pending request that came through the GFS at once (v_84): anyone on a
GFS can ask, so a flood must be clearable in one click. Requests from this
household's people or a paired household are left alone."""

from .test_space_read_gate import (
    _create_space,
    _forbidden,
    seeded,  # noqa: F401 — the shared fixture
)


async def _gfs_request(client, sid: str, rid: str) -> None:
    await client._db.enqueue(
        "INSERT INTO space_join_requests(id, space_id, user_id, expires_at,"
        " remote_applicant_instance_id, gfs_relay)"
        " VALUES(?, ?, ?, '2099-01-01T00:00:00+00:00', 'far-house', ?)",
        (rid, sid, f"u-{rid}", '{"side": "host"}'),
    )


async def _pending(client, sid: str) -> list[str]:
    rows = await client._db.fetchall(
        "SELECT id FROM space_join_requests WHERE space_id=? AND status='pending'"
        " ORDER BY id",
        (sid,),
    )
    return [r["id"] for r in rows]


async def test_an_admin_declines_every_gfs_request_at_once(seeded):  # noqa: F811
    client, hdr = seeded
    sid = await _create_space(client, hdr, "public", "request")
    for rid in ("g1", "g2", "g3"):
        await _gfs_request(client, sid, rid)
    r = await client.post(
        f"/api/spaces/{sid}/join-requests", json={"message": "hi"}, headers=hdr["guest"]
    )
    assert r.status == 201, await r.text()
    (local,) = [x for x in await _pending(client, sid) if not x.startswith("g")]

    r = await client.post(
        f"/api/spaces/{sid}/gfs-join-requests/decline", headers=hdr["owner"]
    )
    assert r.status == 200, await r.text()
    assert await r.json() == {"declined": 3}
    assert await _pending(client, sid) == [local]


async def test_a_non_admin_cannot(seeded):  # noqa: F811
    client, hdr = seeded
    sid = await _create_space(client, hdr, "public", "request")
    await _gfs_request(client, sid, "g1")
    r = await client.post(
        f"/api/spaces/{sid}/gfs-join-requests/decline", headers=hdr["guest"]
    )
    assert r.status == 403
    assert await _pending(client, sid) == ["g1"]
    await _forbidden(
        await client.post(
            "/api/spaces/sp-nope/gfs-join-requests/decline", headers=hdr["guest"]
        )
    )
