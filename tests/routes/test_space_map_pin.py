"""A public space's map pin, edited in Settings › Location.

New spaces no longer collect one (``POST /api/spaces`` ignores lat / lon:
nothing shows a pin); a pin stored before that can be moved or removed.

* ``PATCH /api/spaces/{id}`` with ``{lat, lon}`` moves the pin (4 dp),
  ``{lat: null, lon: null}`` removes it — owner / admin, on a public
  space hosted here (as at creation); anything else is refused.
* The pin is host-local: it bumps no ``config_sequence`` (so nothing
  federates) and ``GET /api/spaces/{id}`` shows it to owners / admins
  only (``map_pin``).
"""

import pytest

from .test_space_read_gate import _add_user, _seat

_OWNER = ("olive", "uid-olive")
_ADMIN = ("adam", "uid-adam")
_MEMBER = ("mia", "uid-mia")


@pytest.fixture
async def space(client):
    hdr = {
        "owner": await _add_user(client, *_OWNER),
        "admin": await _add_user(client, *_ADMIN),
        "member": await _add_user(client, *_MEMBER),
    }
    r = await client.post(
        "/api/spaces",
        json={"name": "Garden", "space_type": "public"},
        headers=hdr["owner"],
    )
    assert r.status == 201, await r.text()
    sid = (await r.json())["id"]
    # A pin stored before create stopped collecting one.
    await client._db.enqueue(
        "UPDATE spaces SET lat=47.3769, lon=8.5417 WHERE id=?", (sid,)
    )
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


async def test_create_collects_no_pin(client, space):
    """Nothing shows a pin, so ``POST /api/spaces`` ignores lat / lon."""
    _, hdr = space
    r = await client.post(
        "/api/spaces",
        json={"name": "Club", "space_type": "public", "lat": 47.1, "lon": 8.1},
        headers=hdr["owner"],
    )
    assert r.status == 201, await r.text()
    sid = (await r.json())["id"]
    row = await client._db.fetchone("SELECT lat, lon FROM spaces WHERE id=?", (sid,))
    assert (row["lat"], row["lon"]) == (None, None)
    assert (await _detail(client, sid, hdr["owner"]))["map_pin"] is None


async def test_owner_and_admins_see_the_pin_members_do_not(client, space):
    sid, hdr = space
    assert (await _detail(client, sid, hdr["owner"]))["map_pin"] == {
        "lat": 47.3769,
        "lon": 8.5417,
    }
    assert (await _detail(client, sid, hdr["admin"]))["map_pin"] is not None
    assert (await _detail(client, sid, hdr["member"]))["map_pin"] is None


async def test_an_admin_moves_the_pin_rounded_to_4dp_without_a_config_edit(
    client, space
):
    sid, hdr = space
    before = await _sequence(client, sid)
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"lat": 46.948123456, "lon": 7.447456789},
        headers=hdr["admin"],
    )
    assert r.status == 200, await r.text()
    assert (await r.json())["map_pin"] == {"lat": 46.9481, "lon": 7.4475}
    assert (await _detail(client, sid, hdr["owner"]))["map_pin"] == {
        "lat": 46.9481,
        "lon": 7.4475,
    }
    # Host-local: no config bump, so nothing federates.
    assert await _sequence(client, sid) == before


async def test_null_removes_the_pin(client, space):
    sid, hdr = space
    r = await client.patch(
        f"/api/spaces/{sid}", json={"lat": None, "lon": None}, headers=hdr["owner"]
    )
    assert r.status == 200, await r.text()
    assert (await r.json())["map_pin"] is None
    assert (await _detail(client, sid, hdr["owner"]))["map_pin"] is None


async def test_a_member_cannot_move_the_pin(client, space):
    sid, hdr = space
    r = await client.patch(
        f"/api/spaces/{sid}", json={"lat": 1.0, "lon": 2.0}, headers=hdr["member"]
    )
    assert r.status == 403
    assert (await _detail(client, sid, hdr["owner"]))["map_pin"]["lat"] == 47.3769


@pytest.mark.parametrize(
    "body",
    [
        {"lat": 91, "lon": 0},
        {"lat": 0, "lon": 181},
        {"lat": "x", "lon": 0},
        {"lat": True, "lon": 0},
        {"lat": 1.0},
        {"lat": None, "lon": 3.0},
    ],
)
async def test_bad_coordinates_are_refused(client, space, body):
    sid, hdr = space
    r = await client.patch(f"/api/spaces/{sid}", json=body, headers=hdr["owner"])
    assert r.status == 422, await r.text()


@pytest.mark.parametrize("tier", ["private", "household", "global"])
async def test_only_a_public_space_has_a_pin(client, space, tier):
    _, hdr = space
    r = await client.post(
        "/api/spaces",
        json={"name": "Family", "space_type": tier, "lat": 1, "lon": 2},
        headers=hdr["owner"],
    )
    assert r.status == 201, await r.text()
    sid = (await r.json())["id"]
    r = await client.patch(
        f"/api/spaces/{sid}", json={"lat": 1.0, "lon": 2.0}, headers=hdr["owner"]
    )
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "MAP_PIN_UNAVAILABLE"
    assert (await _detail(client, sid, hdr["owner"]))["map_pin"] is None


async def test_a_space_without_a_pin_gets_none(client, space):
    """Nothing shows a pin, so a space without one is not given one."""
    sid, hdr = space
    r = await client.patch(
        f"/api/spaces/{sid}", json={"lat": None, "lon": None}, headers=hdr["owner"]
    )
    assert r.status == 200
    r = await client.patch(
        f"/api/spaces/{sid}", json={"lat": 1.0, "lon": 2.0}, headers=hdr["owner"]
    )
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "MAP_PIN_UNAVAILABLE"
    assert (await _detail(client, sid, hdr["owner"]))["map_pin"] is None


async def test_a_pin_left_on_a_space_no_longer_public_can_be_removed(client, space):
    sid, hdr = space
    await client._db.enqueue(
        "UPDATE spaces SET space_type='private' WHERE id=?", (sid,)
    )
    assert (await _detail(client, sid, hdr["owner"]))["map_pin"] is not None
    r = await client.patch(
        f"/api/spaces/{sid}", json={"lat": 1.0, "lon": 2.0}, headers=hdr["owner"]
    )
    assert r.status == 409
    r = await client.patch(
        f"/api/spaces/{sid}", json={"lat": None, "lon": None}, headers=hdr["owner"]
    )
    assert r.status == 200
    assert (await _detail(client, sid, hdr["owner"]))["map_pin"] is None


async def test_a_space_hosted_elsewhere_has_no_pin_to_edit(client, space):
    sid, hdr = space
    await client._db.enqueue(
        "UPDATE spaces SET owner_instance_id='inst-other' WHERE id=?", (sid,)
    )
    r = await client.patch(
        f"/api/spaces/{sid}", json={"lat": 1.0, "lon": 2.0}, headers=hdr["owner"]
    )
    assert r.status == 409
    assert (await _detail(client, sid, hdr["owner"]))["map_pin"] is None


async def test_pin_with_other_fields_saves_both(client, space):
    sid, hdr = space
    r = await client.patch(
        f"/api/spaces/{sid}",
        json={"name": "Allotment", "lat": 1.23456, "lon": 2.34567},
        headers=hdr["owner"],
    )
    assert r.status == 200, await r.text()
    d = await _detail(client, sid, hdr["owner"])
    assert d["name"] == "Allotment"
    assert d["map_pin"] == {"lat": 1.2346, "lon": 2.3457}
