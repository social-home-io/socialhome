"""Local communities over HTTP — the area on create / PATCH / GET, the
directory rows, and the seat routes deciding on the household's home rounded
to ~1 km: refused outside or with no home (403 ``OUTSIDE_AREA`` /
``HOME_NOT_SET``), near the edge a join request for the admins (answered like
any request; ``JOIN_REQUEST_SENT`` where the route would have seated).

* ``POST /api/spaces`` takes ``lat`` / ``lon`` / ``radius_km`` for a public
  space (4 dp centre; 422 on any other tier or a partial / bad area).
* ``PATCH /api/spaces/{id}`` with all three sets or moves the area, all
  three ``null`` clears it — owner / admin, on the household hosting it.
  Host-local: no ``config_sequence`` bump.
* ``GET /api/spaces/{id}`` (member and landing views) and
  ``GET /api/household/spaces`` carry ``local_area`` (the centre — the
  host's own people only). ``GET /api/peer_spaces`` carries
  ``local_community`` (the radius only), never a centre or a verdict.
"""

import pytest

from socialhome.services.space_service import SpaceService

from .test_space_read_gate import _add_user, _seat

_OWNER = ("olive", "uid-olive")
_ADMIN = ("adam", "uid-adam")
_MEMBER = ("mia", "uid-mia")
_NEIGHBOUR = ("ned", "uid-ned")

ZURICH = {"lat": 47.3769, "lon": 8.5417, "radius_km": 5}
NEAR = (47.3920, 8.5600)
EDGE = (47.3769, 8.6082)  # its ~1 km cell straddles the rim
BERN = (46.9481, 7.4474)


async def _set_home(client, home):
    lat, lon = home if home is not None else (None, None)
    await client._db.enqueue(
        "UPDATE instance_identity SET home_lat=?, home_lon=? WHERE id='self'",
        (lat, lon),
    )


@pytest.fixture
async def space(client):
    hdr = {
        "owner": await _add_user(client, *_OWNER),
        "admin": await _add_user(client, *_ADMIN),
        "member": await _add_user(client, *_MEMBER),
        "neighbour": await _add_user(client, *_NEIGHBOUR),
    }
    await _set_home(client, NEAR)
    r = await client.post(
        "/api/spaces",
        json={
            "name": "Quartier",
            "space_type": "public",
            "join_mode": "open",
            **ZURICH,
        },
        headers=hdr["owner"],
    )
    assert r.status == 201, await r.text()
    sid = (await r.json())["id"]
    await _seat(client, sid, _ADMIN[1], "admin")
    await _seat(client, sid, _MEMBER[1], "member")
    return sid, hdr


async def _detail(client, sid, hdr):
    r = await client.get(f"/api/spaces/{sid}", headers=hdr)
    assert r.status == 200, await r.text()
    return await r.json()


async def _sequence(client, sid):
    row = await client._db.fetchone(
        "SELECT config_sequence FROM spaces WHERE id=?", (sid,)
    )
    return row["config_sequence"]


# ── create ──────────────────────────────────────────────────────────────


async def test_create_stores_the_area_and_everyone_sees_it(client, space):
    sid, hdr = space
    want = {"lat": 47.3769, "lon": 8.5417, "radius_km": 5.0}
    for who in ("owner", "admin", "member"):
        assert (await _detail(client, sid, hdr[who]))["local_area"] == want
    # Before joining too: the landing view shows the exact area.
    assert (await _detail(client, sid, hdr["neighbour"]))["local_area"] == want


async def test_create_truncates_to_4dp(client, space):
    _, hdr = space
    r = await client.post(
        "/api/spaces",
        json={
            "name": "Dorf",
            "space_type": "public",
            "lat": 47.376_912_3,
            "lon": 8.541_749_9,
            "radius_km": 2,
        },
        headers=hdr["owner"],
    )
    sid = (await r.json())["id"]
    row = await client._db.fetchone(
        "SELECT lat, lon, radius_km FROM spaces WHERE id=?", (sid,)
    )
    assert (row["lat"], row["lon"], row["radius_km"]) == (47.3769, 8.5417, 2.0)


@pytest.mark.parametrize("tier", ["private", "household", "global"])
async def test_create_refuses_an_area_on_another_tier(client, space, tier):
    _, hdr = space
    r = await client.post(
        "/api/spaces",
        json={"name": "Family", "space_type": tier, **ZURICH},
        headers=hdr["owner"],
    )
    assert r.status == 422, await r.text()
    assert (await r.json())["error"]["code"] == "LOCAL_AREA_PUBLIC_ONLY"


@pytest.mark.parametrize(
    "area",
    [
        {"lat": 47.0, "lon": 8.0},
        {"radius_km": 5},
        {"lat": 47.0, "lon": 8.0, "radius_km": 0.2},
        {"lat": 47.0, "lon": 8.0, "radius_km": 51},
        {"lat": "47", "lon": 8.0, "radius_km": 5},
        {"lat": 47.0, "lon": True, "radius_km": 5},
    ],
)
async def test_create_refuses_a_partial_or_bad_area(client, space, area):
    _, hdr = space
    r = await client.post(
        "/api/spaces",
        json={"name": "Bad", "space_type": "public", **area},
        headers=hdr["owner"],
    )
    assert r.status == 422, await r.text()


# ── PATCH ───────────────────────────────────────────────────────────────


async def test_patch_moves_the_area_without_a_config_bump(client, space):
    sid, hdr = space
    seq = await _sequence(client, sid)
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"lat": 46.948_123, "lon": 7.447_412, "radius_km": 10},
        headers=hdr["admin"],
    )
    assert r.status == 200, await r.text()
    assert (await r.json())["local_area"] == {
        "lat": 46.9481,
        "lon": 7.4474,
        "radius_km": 10.0,
    }
    assert await _sequence(client, sid) == seq


async def test_patch_null_clears_the_area(client, space):
    sid, hdr = space
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"lat": None, "lon": None, "radius_km": None},
        headers=hdr["owner"],
    )
    assert r.status == 200
    assert (await r.json())["local_area"] is None
    assert (await _detail(client, sid, hdr["owner"]))["local_area"] is None


async def test_a_member_cannot_change_the_area(client, space):
    sid, hdr = space
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"lat": None, "lon": None, "radius_km": None},
        headers=hdr["member"],
    )
    assert r.status == 403


@pytest.mark.parametrize(
    "body",
    [
        {"lat": 91, "lon": 0, "radius_km": 5},
        {"lat": 0, "lon": 181, "radius_km": 5},
        {"lat": "x", "lon": 0, "radius_km": 5},
        {"lat": 1.0, "lon": 2.0},
        {"radius_km": 5},
        {"lat": None, "lon": 3.0, "radius_km": None},
        {"lat": 1.0, "lon": 2.0, "radius_km": 0},
    ],
)
async def test_bad_areas_are_refused(client, space, body):
    sid, hdr = space
    r = await client.patch(f"/api/spaces/{sid}", json=body, headers=hdr["owner"])
    assert r.status == 422, await r.text()


async def test_patch_on_another_tier_is_422(client, space):
    _, hdr = space
    r = await client.post("/api/spaces", json={"name": "Family"}, headers=hdr["owner"])
    sid = (await r.json())["id"]
    r = await client.patch(f"/api/spaces/{sid}", json=ZURICH, headers=hdr["owner"])
    assert r.status == 422
    assert (await r.json())["error"]["code"] == "LOCAL_AREA_PUBLIC_ONLY"


async def test_a_space_hosted_elsewhere_has_no_area_to_edit(client, space):
    sid, hdr = space
    await client._db.enqueue(
        "UPDATE spaces SET owner_instance_id='inst-other' WHERE id=?", (sid,)
    )
    r = await client.patch(f"/api/spaces/{sid}", json=ZURICH, headers=hdr["owner"])
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "LOCAL_AREA_UNAVAILABLE"


async def test_area_with_other_fields_saves_both(client, space):
    sid, hdr = space
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"name": "Allmend", **ZURICH, "radius_km": 2},
        headers=hdr["owner"],
    )
    assert r.status == 200, await r.text()
    d = await _detail(client, sid, hdr["owner"])
    assert d["name"] == "Allmend"
    assert d["local_area"]["radius_km"] == 2.0


# ── seat routes ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "home,code",
    [(BERN, "OUTSIDE_AREA"), (None, "HOME_NOT_SET")],
)
async def test_join_is_refused_outside_or_without_a_home(client, space, home, code):
    sid, hdr = space
    await _set_home(client, home)
    r = await client.post(
        f"/api/spaces/{sid}/join-requests", json={}, headers=hdr["neighbour"]
    )
    assert r.status == 403, await r.text()
    body = await r.json()
    assert body["error"]["code"] == code
    assert body["error"]["params"] == {"whose": "own"}


async def test_join_inside_is_seated(client, space):
    sid, hdr = space
    r = await client.post(
        f"/api/spaces/{sid}/join-requests", json={}, headers=hdr["neighbour"]
    )
    assert r.status < 300, await r.text()
    assert (await r.json())["joined"] is True
    row = await client._db.fetchone(
        "SELECT role FROM space_members WHERE space_id=? AND user_id=?",
        (sid, _NEIGHBOUR[1]),
    )
    assert row is not None


async def test_an_edge_join_answers_like_any_request_and_the_admins_see_why(
    client, space
):
    """Open space, home near the edge: the same 201 ``{joined: false}`` a
    request-mode space answers; the admins' queue says "edge" — and nothing
    about the home itself."""
    sid, hdr = space
    await _set_home(client, EDGE)
    r = await client.post(
        f"/api/spaces/{sid}/join-requests", json={}, headers=hdr["neighbour"]
    )
    assert r.status == 201, await r.text()
    body = await r.json()
    assert body["joined"] is False and isinstance(body["request_id"], str)
    r = await client.get(f"/api/spaces/{sid}/join-requests", headers=hdr["owner"])
    assert r.status == 200, await r.text()
    (row,) = await r.json()
    assert row["area_verdict"] == "edge"
    assert not {"lat", "lon", "home_lat", "home_lon", "join_location"} & set(row)
    # Approving seats the neighbour.
    r = await client.post(
        f"/api/spaces/{sid}/join-requests/{row['id']}/approve",
        json={},
        headers=hdr["owner"],
    )
    assert r.status < 300, await r.text()
    seat = await client._db.fetchone(
        "SELECT 1 FROM space_members WHERE space_id=? AND user_id=?",
        (sid, _NEIGHBOUR[1]),
    )
    assert seat is not None


async def test_the_eleventh_join_of_the_day_is_429(client, space):
    """Join → leave ten times, then once more: every successful join spent
    a check, so the answer is 429 LOCAL_AREA_RATE_LIMITED and no seat."""
    sid, hdr = space
    for _ in range(10):
        r = await client.post(
            f"/api/spaces/{sid}/join-requests", json={}, headers=hdr["neighbour"]
        )
        assert r.status < 300, await r.text()
        r = await client.delete(
            f"/api/spaces/{sid}/members/{_NEIGHBOUR[1]}", headers=hdr["neighbour"]
        )
        assert r.status < 300, await r.text()
    r = await client.post(
        f"/api/spaces/{sid}/join-requests", json={}, headers=hdr["neighbour"]
    )
    assert r.status == 429, await r.text()
    error = (await r.json())["error"]
    assert error["code"] == "LOCAL_AREA_RATE_LIMITED"
    assert error["params"] == {"whose": "own"}


async def test_follow_is_refused_outside_and_reviewed_near_the_edge(client, space):
    sid, hdr = space
    await client._db.enqueue("UPDATE spaces SET allow_subscribers=1 WHERE id=?", (sid,))
    await _set_home(client, BERN)
    r = await client.post(f"/api/spaces/{sid}/subscribe", headers=hdr["neighbour"])
    assert r.status == 403, await r.text()
    assert (await r.json())["error"]["code"] == "OUTSIDE_AREA"
    await _set_home(client, EDGE)
    r = await client.post(f"/api/spaces/{sid}/subscribe", headers=hdr["neighbour"])
    assert r.status == 403, await r.text()
    assert (await r.json())["error"]["code"] == "JOIN_REQUEST_SENT"


async def test_invite_link_is_refused_outside_and_reviewed_near_the_edge(client, space):
    sid, hdr = space
    r = await client.post(
        f"/api/spaces/{sid}/invite-tokens", json={"uses": 1}, headers=hdr["owner"]
    )
    assert r.status in (200, 201), await r.text()
    token = (await r.json())["token"]
    await _set_home(client, BERN)
    r = await client.post(
        "/api/spaces/join", json={"token": token}, headers=hdr["neighbour"]
    )
    assert r.status == 403, await r.text()
    assert (await r.json())["error"]["code"] == "OUTSIDE_AREA"
    await _set_home(client, EDGE)
    r = await client.post(
        "/api/spaces/join", json={"token": token}, headers=hdr["neighbour"]
    )
    assert r.status == 403, await r.text()
    assert (await r.json())["error"]["code"] == "JOIN_REQUEST_SENT"


async def test_a_remote_invite_accept_forwards_consent(client, space, monkeypatch):
    """``POST /api/remote_invites/{token}/accept`` passes the person's
    consent through; without it nothing about the home is sent."""
    _, hdr = space
    seen: list[bool] = []

    async def _fake(self, *, token, user_id, share_location=False):
        seen.append(share_location)

    monkeypatch.setattr(SpaceService, "accept_remote_invite", _fake)
    r = await client.post("/api/remote_invites/tok/accept", headers=hdr["neighbour"])
    assert r.status == 204, await r.text()
    r = await client.post(
        "/api/remote_invites/tok/accept",
        json={"share_location": True},
        headers=hdr["neighbour"],
    )
    assert r.status == 204, await r.text()
    assert seen == [False, True]


# ── directory rows ──────────────────────────────────────────────────────


async def test_household_directory_rows_carry_the_area(client, space):
    sid, hdr = space
    r = await client.get("/api/household/spaces", headers=hdr["neighbour"])
    rows = {row["space_id"]: row for row in await r.json()}
    assert rows[sid]["local_area"] == {
        "lat": 47.3769,
        "lon": 8.5417,
        "radius_km": 5.0,
    }


async def _peer(client, iid: str) -> None:
    await client._db.enqueue(
        """
        INSERT INTO remote_instances(
            id, display_name, remote_identity_pk, key_self_to_remote,
            key_remote_to_self, remote_inbox_url, local_inbox_id, status,
            source
        ) VALUES(?, ?, ?, '00', '00', ?, ?, 'confirmed', 'manual')
        """,
        (
            iid,
            iid.title(),
            "ab" * 32,
            f"https://{iid}.example/inbox/x",
            f"{iid}_local",
        ),
    )


async def test_peer_directory_rows_carry_the_radius_only(client, space):
    """``GET /api/peer_spaces``: a paired household's local community is
    "within X km" — no verdict, no sharing state, no coordinates."""
    _, hdr = space
    await _peer(client, "berta")
    await _peer(client, "carlo")
    await client._db.enqueue(
        "INSERT INTO peer_space_directory(instance_id, space_id, name,"
        " radius_km) VALUES('berta', 'sp-b', 'Nachbarn', 2.0)"
    )
    await client._db.enqueue(
        "INSERT INTO peer_space_directory(instance_id, space_id, name)"
        " VALUES('carlo', 'sp-c', 'Chess')"
    )
    r = await client.get("/api/peer_spaces", headers=hdr["neighbour"])
    assert r.status == 200
    body = await r.json()
    rows = {row["space_id"]: row for row in body}
    assert rows["sp-b"]["local_community"] == {"radius_km": 2.0}
    assert rows["sp-c"]["local_community"] is None
    for row in body:
        assert not {"lat", "lon", "local_area", "home_shared_with_host"} & set(row)
