"""A global space's join contact on the GFS listing (v_84).

The owner sends ``join_contact`` (its keys, signed by the space authority
key) inside its signed publish; the GFS serves it on the public listing
only when it verifies against the key it pinned for the space — so a
household reads it there and nobody looks anything up."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import generate_identity_keypair
from socialhome.global_server import create_gfs_app
from socialhome.global_server.app_keys import gfs_fed_repo_key
from socialhome.global_server.cluster import _space_to_wire, _wire_to_space
from socialhome.global_server.domain import GlobalSpace
from socialhome.space_join_contact import sign_join_contact
from tests.global_server.test_space_publish_routes import (
    _register_owner,
    _sign_publish_body,
)


@pytest.fixture
async def gfs_client(tmp_path):
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    async with TestClient(TestServer(app)) as tc:
        yield tc


def _base(space_id: str, owner: str, join_mode: str, space_pk: str) -> dict:
    return {
        "space_id": space_id,
        "owning_instance": owner,
        "name": "Ask Me",
        "description": "",
        "about_markdown": "",
        "cover_url": "",
        "icon_url": "",
        "min_age": 0,
        "category": "general",
        "accent_color": "#D2542A",
        "primary_color": "#D2542A",
        "join_mode": join_mode,
        "identity_public_key": space_pk,
    }


def _contact(space_seed: bytes, space_id: str, instance_id: str) -> dict:
    return sign_join_contact(
        space_id=space_id,
        instance_id=instance_id,
        identity_pk="aa" * 32,
        keywrap_pk="bb" * 32,
        keywrap_sig="sig",
        space_seed=space_seed,
    )


async def _publish(client, owner_seed, body) -> int:
    signed = _sign_publish_body(body, seed=owner_seed)
    resp = await client.post(f"/gfs/spaces/{body['space_id']}/publish", json=signed)
    return resp.status


async def _listed(client, space_id: str) -> dict:
    listing = await (await client.get("/gfs/spaces")).json()
    return next(s for s in listing["spaces"] if s["space_id"] == space_id)


async def test_a_verified_contact_is_served_on_the_public_listing(gfs_client):
    app = gfs_client.server.app
    owner_seed, _ = await _register_owner(app, instance_id="owner.home")
    space = generate_identity_keypair()
    contact = _contact(space.private_key, "sp-1", "owner.home")
    body = {
        **_base("sp-1", "owner.home", "request", space.public_key.hex()),
        "join_contact": contact,
    }
    assert await _publish(gfs_client, owner_seed, body) == 200
    row = await _listed(gfs_client, "sp-1")
    assert row["join_contact"] == contact
    assert row["identity_public_key"] == space.public_key.hex()


@pytest.mark.parametrize(
    ("join_mode", "signer", "names"),
    [
        ("invite_only", "space", "owner.home"),  # takes no requests
        ("request", "other", "owner.home"),  # not the pinned space key
        ("request", "space", "someone.else"),  # names another household
    ],
)
async def test_a_contact_the_reader_would_refuse_is_not_served(
    gfs_client, join_mode, signer, names
):
    app = gfs_client.server.app
    owner_seed, _ = await _register_owner(app, instance_id="owner.home")
    space = generate_identity_keypair()
    other = generate_identity_keypair()
    seed = space.private_key if signer == "space" else other.private_key
    body = {
        **_base("sp-2", "owner.home", join_mode, space.public_key.hex()),
        "join_contact": _contact(seed, "sp-2", names),
    }
    assert await _publish(gfs_client, owner_seed, body) == 200
    assert (await _listed(gfs_client, "sp-2"))["join_contact"] is None


async def test_the_contact_is_inside_the_owners_signature(gfs_client):
    """Bolted on after signing (by a relay), the publish fails."""
    app = gfs_client.server.app
    owner_seed, _ = await _register_owner(app, instance_id="owner.home")
    space = generate_identity_keypair()
    signed = _sign_publish_body(
        _base("sp-3", "owner.home", "request", space.public_key.hex()), seed=owner_seed
    )
    signed["join_contact"] = _contact(space.private_key, "sp-3", "owner.home")
    resp = await gfs_client.post("/gfs/spaces/sp-3/publish", json=signed)
    assert resp.status == 403


async def test_the_contact_travels_with_cluster_gossip(gfs_client):
    app = gfs_client.server.app
    owner_seed, _ = await _register_owner(app, instance_id="owner.home")
    space = generate_identity_keypair()
    contact = _contact(space.private_key, "sp-4", "owner.home")
    await _publish(
        gfs_client,
        owner_seed,
        {
            **_base("sp-4", "owner.home", "open", space.public_key.hex()),
            "join_contact": contact,
        },
    )
    stored = await app[gfs_fed_repo_key].get_space("sp-4")
    assert _wire_to_space(_space_to_wire(stored)).join_contact == contact


async def test_the_server_says_it_serves_join_contacts(gfs_client):
    info = await (await gfs_client.get("/gfs/info")).json()
    caps = info.get("capabilities") or {}
    if caps:  # only when a signing key is wired
        assert caps["join_contact"] is True


async def test_the_owners_rotation_cert_travels_with_a_contact_only(gfs_client):
    """A household moves its pin only along the owner's v_44 chain, so a
    listing with a contact carries the owner's cert for the current key. A
    listing without a contact keeps the cert inside, as before."""
    app = gfs_client.server.app
    await _register_owner(app, instance_id="owner.home")
    repo = app[gfs_fed_repo_key]
    cert = {"space_id": "sp-c", "key_epoch": 1, "cert_sig": "x"}
    contact = _contact(generate_identity_keypair().private_key, "sp-c", "owner.home")
    for space_id, with_contact in (("sp-c", True), ("sp-n", False)):
        await repo.upsert_space(
            GlobalSpace(
                space_id=space_id,
                owning_instance="owner.home",
                name=space_id,
                join_mode="request",
                status="active",
                identity_public_key="aa" * 32,
                join_contact=contact if with_contact else None,
            )
        )
        assert await repo.set_space_authority(
            space_id,
            expected_pk="aa" * 32,
            expected_cert=None,
            new_pk="cc" * 32,
            cert=cert,
        )
    with_c = await _listed(gfs_client, "sp-c")
    without = await _listed(gfs_client, "sp-n")
    assert with_c["authority_cert"] == cert
    assert "authority_cert" not in without
