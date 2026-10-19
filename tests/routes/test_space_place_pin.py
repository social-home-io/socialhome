"""A global space's place pin over HTTP: ``POST /api/spaces`` and
``PATCH /api/spaces/{id}`` take ``place: {lat, lon}`` (global only, 4 dp,
``null`` clears), ``GET /api/spaces/{id}`` returns it. No config bump —
it is host-local listing metadata."""

import pytest

from .test_space_read_gate import _add_user, _seat

_OWNER = ("olive", "uid-olive")
_MEMBER = ("mia", "uid-mia")


@pytest.fixture
async def space(client):
    hdr = {
        "owner": await _add_user(client, *_OWNER),
        "member": await _add_user(client, *_MEMBER),
    }
    r = await client.post(
        "/api/spaces",
        json={
            "name": "Chess Zürich",
            "space_type": "global",
            "join_mode": "request",
            "place": {"lat": 47.376_912_3, "lon": 8.541_749_9},
        },
        headers=hdr["owner"],
    )
    assert r.status == 201, await r.text()
    sid = (await r.json())["id"]
    await _seat(client, sid, _MEMBER[1], "member")
    return sid, hdr


async def _detail(client, sid, hdr):
    r = await client.get(f"/api/spaces/{sid}", headers=hdr)
    assert r.status == 200, await r.text()
    return await r.json()


async def test_create_stores_the_pin_at_4dp(client, space):
    sid, hdr = space
    assert (await _detail(client, sid, hdr["owner"]))["place"] == {
        "lat": 47.3769,
        "lon": 8.5417,
    }
    row = await client._db.fetchone(
        "SELECT lat, lon, radius_km FROM spaces WHERE id=?", (sid,)
    )
    assert (row["lat"], row["lon"], row["radius_km"]) == (47.3769, 8.5417, None)


async def test_patch_moves_and_clears_without_a_config_bump(client, space):
    sid, hdr = space
    before = (
        await client._db.fetchone(
            "SELECT config_sequence FROM spaces WHERE id=?", (sid,)
        )
    )["config_sequence"]
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"place": {"lat": 46.9481, "lon": 7.4474}},
        headers=hdr["owner"],
    )
    assert r.status == 200, await r.text()
    assert (await r.json())["place"] == {"lat": 46.9481, "lon": 7.4474}
    r = await client.patch(
        f"/api/spaces/{sid}", json={"place": None}, headers=hdr["owner"]
    )
    assert (await r.json())["place"] is None
    assert (await _detail(client, sid, hdr["owner"]))["place"] is None
    after = (
        await client._db.fetchone(
            "SELECT config_sequence FROM spaces WHERE id=?", (sid,)
        )
    )["config_sequence"]
    assert after == before


async def test_a_member_cannot_move_it(client, space):
    sid, hdr = space
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"place": {"lat": 1, "lon": 2}},
        headers=hdr["member"],
    )
    assert r.status == 403


@pytest.mark.parametrize("tier", ["public", "private", "household"])
async def test_create_on_another_tier_is_422(client, space, tier):
    _, hdr = space
    r = await client.post(
        "/api/spaces",
        json={"name": "X", "space_type": tier, "place": {"lat": 1, "lon": 2}},
        headers=hdr["owner"],
    )
    assert r.status == 422
    assert (await r.json())["error"]["code"] == "PLACE_PIN_GLOBAL_ONLY"


@pytest.mark.parametrize(
    "bad", [{"lat": 91, "lon": 0}, {"lat": "47", "lon": 8}, [47, 8], {"lat": 47}]
)
async def test_a_malformed_pin_is_422(client, space, bad):
    sid, hdr = space
    r = await client.patch(
        f"/api/spaces/{sid}", json={"place": bad}, headers=hdr["owner"]
    )
    assert r.status == 422


async def test_the_radius_hint_round_trips_and_can_be_dropped(client, space):
    """``place: {lat, lon, radius_km}`` — a hint, so nothing else changes."""
    sid, hdr = space
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"place": {"lat": 47.3769, "lon": 8.5417, "radius_km": 10}},
        headers=hdr["owner"],
    )
    assert r.status == 200, await r.text()
    want = {"lat": 47.3769, "lon": 8.5417, "radius_km": 10.0}
    assert (await r.json())["place"] == want
    assert (await _detail(client, sid, hdr["owner"]))["place"] == want
    # Members read it too: it is the space's published listing metadata.
    assert (await _detail(client, sid, hdr["member"]))["place"] == want
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"place": {"lat": 47.3769, "lon": 8.5417}},
        headers=hdr["owner"],
    )
    assert (await r.json())["place"] == {"lat": 47.3769, "lon": 8.5417}


@pytest.mark.parametrize("bad", [0.2, 51, "10", True])
async def test_a_hint_out_of_bounds_is_422(client, space, bad):
    sid, hdr = space
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"place": {"lat": 47.0, "lon": 8.0, "radius_km": bad}},
        headers=hdr["owner"],
    )
    assert r.status == 422
