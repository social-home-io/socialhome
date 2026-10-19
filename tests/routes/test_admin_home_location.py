"""``/api/admin/home-location`` — Admin › Settings › Our home location.

Standalone (the test app's mode) sets its home here; a platform that owns
the home (Home Assistant) is read-only — exercised with the real service
over a stub :class:`HomeLocationSource`.
"""

from __future__ import annotations

from socialhome.app_keys import (
    event_bus_key,
    federation_repo_key,
    home_location_service_key,
)
from socialhome.domain.events import LocalHomeLocationUpdated
from socialhome.platform.adapter import Capability
from socialhome.services.home_location_service import HomeLocationService

from .conftest import _auth

URL = "/api/admin/home-location"


async def _demote(client) -> None:
    await client._db.enqueue(
        "UPDATE users SET is_admin=0 WHERE user_id=?",
        (client._uid,),
    )


async def test_admin_only(client):
    await _demote(client)
    for method in ("get", "put", "delete"):
        resp = await getattr(client, method)(
            URL, headers=_auth(client._tok), json={"latitude": 1.0, "longitude": 2.0}
        )
        assert resp.status == 403, method


async def test_needs_a_session(client):
    resp = await client.get(URL)
    assert resp.status == 401


async def test_standalone_has_no_platform_home(client):
    """The SPA shows the editor off the absent capability."""
    resp = await client.get("/api/instance/config")
    caps = (await resp.json())["capabilities"]
    assert Capability.HOME_LOCATION_SYNC.value not in caps


async def test_set_read_clear_round_trip(client):
    received: list[LocalHomeLocationUpdated] = []

    async def _record(event):
        received.append(event)

    client.app[event_bus_key].subscribe(LocalHomeLocationUpdated, _record)

    resp = await client.get(URL, headers=_auth(client._tok))
    assert resp.status == 200
    assert await resp.json() == {
        "latitude": None,
        "longitude": None,
        "from_platform": False,
    }

    resp = await client.put(
        URL,
        headers=_auth(client._tok),
        json={"latitude": 47.376912345, "longitude": 8.541698765},
    )
    assert resp.status == 200
    assert await resp.json() == {
        "latitude": 47.3769,
        "longitude": 8.5417,
        "from_platform": False,
    }
    identity = await client.app[federation_repo_key].get_local_identity()
    assert (identity["home_lat"], identity["home_lon"]) == (47.3769, 8.5417)
    # ``/api/friends`` (the SPA's household-home read) carries it at once.
    friends = await (
        await client.get("/api/friends", headers=_auth(client._tok))
    ).json()
    assert (friends["instance"]["home_lat"], friends["instance"]["home_lon"]) == (
        47.3769,
        8.5417,
    )

    resp = await client.delete(URL, headers=_auth(client._tok))
    assert resp.status == 200
    assert (await resp.json())["latitude"] is None
    identity = await client.app[federation_repo_key].get_local_identity()
    assert identity["home_lat"] is None and identity["home_lon"] is None
    assert [(e.latitude, e.longitude) for e in received] == [
        (47.3769, 8.5417),
        (None, None),
    ]


async def test_rejects_an_unusable_point(client):
    for body in (
        {"latitude": 91, "longitude": 8.5},
        {"latitude": "47.3", "longitude": 8.5},
        {"latitude": 47.3},
        {"latitude": 0, "longitude": 0},
    ):
        resp = await client.put(URL, headers=_auth(client._tok), json=body)
        assert resp.status == 422, body
        assert (await resp.json())["error"]["code"] == "INVALID_HOME_LOCATION"
    resp = await client.put(URL, headers=_auth(client._tok), json=[47.3, 8.5])
    assert resp.status == 422


class _Source:
    """A platform that owns the home (Home Assistant's ``/api/config``)."""

    async def get_home_location(self):
        return (52.52, 13.405)


async def test_platform_home_is_read_only(make_client):
    # Built against the started app's own repo + bus.
    client = await make_client(
        lambda app: {
            home_location_service_key: HomeLocationService(
                app[federation_repo_key], bus=app[event_bus_key], source=_Source()
            )
        }
    )
    resp = await client.get(URL, headers=_auth(client._tok))
    assert resp.status == 200
    assert await resp.json() == {
        "latitude": 52.52,
        "longitude": 13.405,
        "from_platform": True,
    }
    resp = await client.put(
        URL, headers=_auth(client._tok), json={"latitude": 47.3769, "longitude": 8.5417}
    )
    assert resp.status == 409
    assert (await resp.json())["error"]["code"] == "HOME_LOCATION_FROM_PLATFORM"
    resp = await client.delete(URL, headers=_auth(client._tok))
    assert resp.status == 409
    identity = await client.app[federation_repo_key].get_local_identity()
    assert (identity["home_lat"], identity["home_lon"]) == (52.52, 13.405)
