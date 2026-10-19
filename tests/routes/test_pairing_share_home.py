"""``POST /api/pairing/initiate`` / ``accept`` honour the home-sharing switch.

The pairing code and the scanner's peer-accept carry this household's
home location only when the admin shares it with that household: the
body's ``share_home`` when given, else the household's
``share_home_default`` (on unless an admin turned it off).
"""

from __future__ import annotations

from socialhome.app_keys import federation_repo_key, federation_service_key
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)

from .conftest import _auth


async def _set_home(client) -> None:
    await client._db.enqueue(
        "UPDATE instance_identity SET home_lat=?, home_lon=? WHERE id='self'",
        (52.52, 13.405),
    )


async def _set_default(client, value: bool) -> None:
    r = await client.put(
        "/api/household/preferences",
        json={"toggles": {"share_home_default": value}},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert (await r.json())["share_home_default"] is value


async def _initiate(client, body: dict | None = None):
    return await client.post(
        "/api/pairing/initiate",
        json=body or {},
        headers=_auth(client._tok),
    )


class _Recorder:
    def __init__(self) -> None:
        self.bodies: list[dict] = []

    async def send_peer_accept(self, *, peer_inbox_url, body):
        self.bodies.append(body)

        class _R:
            ok = True
            status_code = 200
            error = None

        return _R()


def _recorder(client) -> _Recorder:
    rec = _Recorder()
    client.server.app[federation_service_key]._pairing.attach_peer_pairing_client(rec)
    return rec


def _code(token: str) -> tuple[dict, str]:
    peer = generate_identity_keypair()
    iid = derive_instance_id(peer.public_key)
    return (
        {
            "token": token,
            "instance_id": iid,
            "identity_pk": peer.public_key.hex(),
            "dh_pk": generate_x25519_keypair().public_key.hex(),
            "inbox_url": "https://peer.example/federation/inbox/abc",
            "display_name": "Peer",
        },
        iid,
    )


# ── Household default ─────────────────────────────────────────────────


async def test_household_default_is_on(client):
    r = await client.get("/api/household/preferences", headers=_auth(client._tok))
    assert (await r.json())["share_home_default"] is True


# ── The code ──────────────────────────────────────────────────────────


async def test_code_carries_home_by_default(client):
    await _set_home(client)
    r = await _initiate(client)
    assert r.status == 201
    data = await r.json()
    assert data["home_lat"] == 52.52
    assert data["home_lon"] == 13.405


async def test_code_has_no_home_when_the_household_default_is_off(client):
    await _set_home(client)
    await _set_default(client, False)
    data = await (await _initiate(client)).json()
    assert "home_lat" not in data
    assert "home_lon" not in data
    session = await client.server.app[federation_repo_key].get_pairing(data["token"])
    assert session.share_home is False


async def test_code_follows_the_dialogs_switch_over_the_default(client):
    await _set_home(client)
    data = await (await _initiate(client, {"share_home": False})).json()
    assert "home_lat" not in data
    await _set_default(client, False)
    data = await (await _initiate(client, {"share_home": True})).json()
    assert data["home_lat"] == 52.52


async def test_code_rejects_a_non_boolean_switch(client):
    r = await _initiate(client, {"share_home": "yes"})
    assert r.status == 422


# ── The scanner's peer-accept ─────────────────────────────────────────


async def test_accept_shares_home_by_default(client):
    await _set_home(client)
    rec = _recorder(client)
    code, iid = _code("tok-default-on")
    r = await client.post("/api/pairing/accept", json=code, headers=_auth(client._tok))
    assert r.status == 200
    assert rec.bodies[0]["home_lat"] == 52.52
    inst = await client.server.app[federation_repo_key].get_instance(iid)
    assert inst.share_home is True


async def test_accept_has_no_home_when_switched_off(client):
    await _set_home(client)
    rec = _recorder(client)
    code, iid = _code("tok-off")
    r = await client.post(
        "/api/pairing/accept",
        json={**code, "share_home": False},
        headers=_auth(client._tok),
    )
    assert r.status == 200
    assert "home_lat" not in rec.bodies[0]
    assert "share_home" not in rec.bodies[0]
    inst = await client.server.app[federation_repo_key].get_instance(iid)
    assert inst.share_home is False


async def test_accept_has_no_home_when_the_household_default_is_off(client):
    await _set_home(client)
    await _set_default(client, False)
    rec = _recorder(client)
    code, iid = _code("tok-default-off")
    r = await client.post("/api/pairing/accept", json=code, headers=_auth(client._tok))
    assert r.status == 200
    assert "home_lat" not in rec.bodies[0]
    inst = await client.server.app[federation_repo_key].get_instance(iid)
    assert inst.share_home is False


async def test_accept_rejects_a_non_boolean_switch(client):
    rec = _recorder(client)
    code, iid = _code("tok-bad")
    r = await client.post(
        "/api/pairing/accept",
        json={**code, "share_home": 1},
        headers=_auth(client._tok),
    )
    assert r.status == 422
    assert rec.bodies == []
    assert await client.server.app[federation_repo_key].get_instance(iid) is None
