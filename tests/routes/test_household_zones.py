"""Integration tests for ``/api/household/zones`` — real SQLite + aiohttp
``TestClient`` through the central error map — and the server-side GPS
match on ``POST /api/presence/location``."""

from __future__ import annotations

import pytest

from socialhome.app_keys import household_zone_service_key
from socialhome.auth import sha256_token_hash
from socialhome.domain.household_zone import MAX_HOUSEHOLD_ZONES, ExternalHouseholdZone

from .conftest import _auth

_URL = "/api/household/zones"

_KEYS = {
    "id",
    "name",
    "latitude",
    "longitude",
    "radius_m",
    "color",
    "icon",
    "is_home",
    "external_id",
    "read_only",
    "created_at",
    "updated_at",
}


async def _member_token(client) -> str:
    await client._db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        ("pascal", "uid-pascal", "Pascal"),
    )
    await client._db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        ("t-member", "uid-pascal", "t", sha256_token_hash("member-tok")),
    )
    return "member-tok"


async def _create(client, name="School", **kw):
    body = {"name": name, "latitude": 52.53, "longitude": 13.405, "radius_m": 200}
    body.update(kw)
    r = await client.post(_URL, json=body, headers=_auth(client._tok))
    return r


async def _created(client, name="School", **kw) -> dict:
    r = await _create(client, name, **kw)
    assert r.status == 201, await r.text()
    return await r.json()


async def _code(r, status: int) -> str:
    assert r.status == status, await r.text()
    return (await r.json())["error"]["code"]


async def _mirror(client, *zones: ExternalHouseholdZone) -> None:
    await client.app[household_zone_service_key].mirror_external(list(zones))


# ─── list / create ───────────────────────────────────────────────────────


async def test_list_empty(client):
    r = await client.get(_URL, headers=_auth(client._tok))
    assert r.status == 200
    assert await r.json() == {"zones": []}


async def test_unauthenticated_is_401(client):
    r = await client.get(_URL)
    assert r.status == 401


async def test_admin_creates_and_member_lists(client):
    z = await _created(
        client,
        "School",
        latitude=52.530049,
        color="#AABBCC",
        icon="🏫",
    )
    assert set(z) == _KEYS
    assert z["id"].startswith("hz_")
    assert z["latitude"] == 52.53
    assert z["color"] == "#aabbcc" and z["icon"] == "🏫"
    assert z["is_home"] is False
    assert z["external_id"] is None and z["read_only"] is False

    tok = await _member_token(client)
    r = await client.get(_URL, headers=_auth(tok))
    assert r.status == 200
    assert (await r.json())["zones"] == [z]


async def test_list_home_first(client):
    await _created(client, "Aunt")
    await _created(client, "Zuhause", is_home=True, latitude=52.52)
    r = await client.get(_URL, headers=_auth(client._tok))
    names = [z["name"] for z in (await r.json())["zones"]]
    assert names == ["Zuhause", "Aunt"]


async def test_member_cannot_write(client):
    z = await _created(client)
    tok = await _member_token(client)
    r = await client.post(
        _URL,
        json={"name": "X", "latitude": 1, "longitude": 2, "radius_m": 100},
        headers=_auth(tok),
    )
    assert await _code(r, 403) == "FORBIDDEN"
    r = await client.patch(f"{_URL}/{z['id']}", json={"name": "Y"}, headers=_auth(tok))
    assert await _code(r, 403) == "FORBIDDEN"
    r = await client.delete(f"{_URL}/{z['id']}", headers=_auth(tok))
    assert await _code(r, 403) == "FORBIDDEN"


@pytest.mark.parametrize(
    "bad",
    [
        {"radius_m": 10},
        {"radius_m": 50_001},
        {"color": "blue"},
        {"icon": "🏫🏠"},
        {"icon": "ab"},
        {"latitude": 95},
        {"latitude": True},
        {"name": ""},
        {"is_home": "true"},
    ],
)
async def test_invalid_fields_are_422(client, bad):
    r = await _create(client, **bad)
    assert r.status == 422, await r.text()


async def test_name_taken_case_insensitive(client):
    await _created(client, "School")
    r = await _create(client, "school")
    assert await _code(r, 409) == "ZONE_NAME_TAKEN"


async def test_zone_cap(client):
    for i in range(MAX_HOUSEHOLD_ZONES):
        await _created(client, f"Z{i}")
    r = await _create(client, "One more")
    assert r.status == 409
    body = await r.json()
    assert body["error"]["code"] == "ZONE_LIMIT"
    assert body["error"]["params"] == {"max": MAX_HOUSEHOLD_ZONES}


async def test_bad_json_is_400(client):
    r = await client.post(
        _URL,
        data="{",
        headers={**_auth(client._tok), "Content-Type": "application/json"},
    )
    assert r.status == 400


# ─── update / delete ─────────────────────────────────────────────────────


async def test_patch_partial_tri_state(client):
    z = await _created(client, color="#112233", icon="🏫")
    r = await client.patch(
        f"{_URL}/{z['id']}",
        json={"radius_m": 300, "color": None, "name": "Big school"},
        headers=_auth(client._tok),
    )
    assert r.status == 200, await r.text()
    out = await r.json()
    assert out["radius_m"] == 300 and out["color"] is None
    assert out["icon"] == "🏫" and out["name"] == "Big school"
    assert out["created_at"] == z["created_at"]


async def test_patch_unknown_is_404(client):
    r = await client.patch(
        f"{_URL}/nope", json={"name": "X"}, headers=_auth(client._tok)
    )
    assert await _code(r, 404) == "NOT_FOUND"


async def test_patch_invalid_is_422(client):
    z = await _created(client)
    r = await client.patch(
        f"{_URL}/{z['id']}", json={"icon": "not emoji"}, headers=_auth(client._tok)
    )
    assert r.status == 422


async def test_delete(client):
    z = await _created(client)
    r = await client.delete(f"{_URL}/{z['id']}", headers=_auth(client._tok))
    assert r.status == 204
    r = await client.get(_URL, headers=_auth(client._tok))
    assert (await r.json())["zones"] == []
    r = await client.delete(f"{_URL}/{z['id']}", headers=_auth(client._tok))
    assert await _code(r, 404) == "NOT_FOUND"


async def test_only_one_home(client):
    old = await _created(client, "Old home", is_home=True)
    new = await _created(client, "New home", is_home=True, latitude=48.1)
    r = await client.get(_URL, headers=_auth(client._tok))
    zones = (await r.json())["zones"]
    assert [z["id"] for z in zones if z["is_home"]] == [new["id"]]
    assert zones[0]["id"] == new["id"]
    assert old["id"] in {z["id"] for z in zones}


async def test_home_cannot_be_deleted_or_unmarked(client):
    home = await _created(client, "Home", is_home=True)
    r = await client.delete(f"{_URL}/{home['id']}", headers=_auth(client._tok))
    assert await _code(r, 409) == "ZONE_IS_HOME"
    r = await client.patch(
        f"{_URL}/{home['id']}", json={"is_home": False}, headers=_auth(client._tok)
    )
    assert await _code(r, 409) == "ZONE_IS_HOME"


async def test_external_zone_is_read_only(client):
    await _mirror(
        client,
        ExternalHouseholdZone(
            external_id="zone.home",
            name="Home",
            latitude=52.52,
            longitude=13.405,
            radius_m=100,
            is_home=True,
        ),
    )
    r = await client.get(_URL, headers=_auth(client._tok))
    [home] = (await r.json())["zones"]
    assert home["external_id"] == "zone.home" and home["read_only"] is True
    r = await client.patch(
        f"{_URL}/{home['id']}", json={"name": "X"}, headers=_auth(client._tok)
    )
    assert await _code(r, 409) == "ZONE_READ_ONLY"
    r = await client.delete(f"{_URL}/{home['id']}", headers=_auth(client._tok))
    assert await _code(r, 409) == "ZONE_READ_ONLY"
    # A zone created here can't take the Home flag from the synced Home.
    r = await _create(client, "Mine", is_home=True)
    assert await _code(r, 409) == "ZONE_READ_ONLY"


async def test_feature_off_is_403(client):
    r = await client.put(
        "/api/household/preferences",
        json={"toggles": {"feat_presence": False}},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    r = await client.get(_URL, headers=_auth(client._tok))
    assert await _code(r, 403) == "FEATURE_DISABLED"
    r = await _create(client)
    assert await _code(r, 403) == "FEATURE_DISABLED"


# ─── POST /api/presence/location — server-side GPS match ─────────────────


async def _push(client, **body) -> dict:
    payload = {"username": "admin", "accuracy_m": 10, **body}
    r = await client.post(
        "/api/presence/location", json=payload, headers=_auth(client._tok)
    )
    assert r.status == 204, await r.text()
    r = await client.get("/api/presence", headers=_auth(client._tok))
    [me] = [p for p in await r.json() if p["username"] == "admin"]
    return me


@pytest.fixture
async def zoned(client):
    await _created(client, "Zuhause", is_home=True, latitude=52.52, radius_m=100)
    await _created(client, "School", latitude=52.53, radius_m=200)
    return client


async def test_gps_in_home_zone_reads_home(zoned):
    me = await _push(zoned, latitude=52.5201, longitude=13.405)
    assert (me["state"], me["zone_name"]) == ("home", "home")


async def test_gps_in_named_zone_reads_zone(zoned):
    me = await _push(zoned, latitude=52.5301, longitude=13.405)
    assert (me["state"], me["zone_name"]) == ("zone", "School")


async def test_gps_outside_reads_away(zoned):
    me = await _push(zoned, latitude=48.0, longitude=11.0)
    assert (me["state"], me["zone_name"]) == ("away", None)


async def test_client_zone_name_wins(zoned):
    me = await _push(zoned, latitude=52.5301, longitude=13.405, zone_name="Work")
    assert (me["state"], me["zone_name"]) == ("zone", "Work")


async def test_poor_accuracy_guesses_nothing(zoned):
    me = await _push(zoned, latitude=52.5201, longitude=13.405, accuracy_m=800)
    assert (me["state"], me["zone_name"]) == ("away", None)
    assert me["latitude"] is None


async def test_non_object_body_is_422(client):
    r = await client.post(_URL, json=[1, 2], headers=_auth(client._tok))
    assert r.status == 422


async def test_null_zone_name_key_means_not_in_a_zone(zoned):
    """The HA integration always sends the ``zone_name`` key — null for
    HA's not_home / unknown / unavailable. That is a statement, not a
    gap: no server-side match."""
    me = await _push(zoned, latitude=52.5201, longitude=13.405, zone_name=None)
    assert (me["state"], me["zone_name"]) == ("away", None)


async def test_explicit_state_is_kept(zoned):
    me = await _push(zoned, latitude=52.5201, longitude=13.405, state="away")
    assert (me["state"], me["zone_name"]) == ("away", None)
