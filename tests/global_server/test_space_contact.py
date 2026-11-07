"""``POST /gfs/spaces/{id}/contact`` — a registered household gets a listed
space owner's sealing keys so it can ask to join over the envelope relay
(capability ``space_contact``, v_84 on the household side)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import b64url_encode, generate_identity_keypair, sign_ed25519
from socialhome.global_server.app_keys import gfs_fed_repo_key
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.server import create_gfs_app


def _body(seed: bytes, *, who: str, space_id: str, ts: str | None = None) -> dict:
    ts = ts or datetime.now(UTC).isoformat()
    canonical = json.dumps(
        {
            "action": "space_contact",
            "instance_id": who,
            "space_id": space_id,
            "ts": ts,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "instance_id": who,
        "ts": ts,
        "signature": b64url_encode(sign_ed25519(seed, canonical)),
    }


@pytest.fixture
async def client(tmp_dir):
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-node-a",
        cluster_enabled=False,
        cluster_node_id="gfs-node-a",
        cluster_peers=(),
    )
    app = create_gfs_app(cfg)
    asker = generate_identity_keypair()
    owner = generate_identity_keypair()
    async with TestClient(TestServer(app)) as tc:
        repo = app[gfs_fed_repo_key]
        await repo.upsert_instance(
            ClientInstance(
                instance_id="asker.home",
                display_name="Asker",
                public_key=asker.public_key.hex(),
                status="active",
            )
        )
        await repo.upsert_instance(
            ClientInstance(
                instance_id="owner.home",
                display_name="Owner",
                public_key=owner.public_key.hex(),
                status="active",
                keywrap_public_key="ab" * 32,
                kem_suite="x25519",
                keywrap_sig="sig-over-keywrap",
            )
        )
        for space_id, mode in (
            ("sp-request", "request"),
            ("sp-open", "open"),
            ("sp-invite", "invite_only"),
        ):
            await repo.upsert_space(
                GlobalSpace(
                    space_id=space_id,
                    owning_instance="owner.home",
                    name=space_id,
                    join_mode=mode,
                    status="active",
                )
            )
        tc.asker_seed = asker.private_key
        tc.owner_seed = owner.private_key
        tc.owner_pk = owner.public_key.hex()
        tc.repo = repo
        yield tc


async def test_a_registered_household_gets_the_owners_registered_keys(client):
    resp = await client.post(
        "/gfs/spaces/sp-request/contact",
        json=_body(client.asker_seed, who="asker.home", space_id="sp-request"),
    )
    assert resp.status == 200
    assert await resp.json() == {
        "instance_id": "owner.home",
        "identity_public_key": client.owner_pk,
        "keywrap_public_key": "ab" * 32,
        "kem_suite": "x25519",
        "keywrap_sig": "sig-over-keywrap",
    }


async def test_an_open_space_answers_too(client):
    resp = await client.post(
        "/gfs/spaces/sp-open/contact",
        json=_body(client.asker_seed, who="asker.home", space_id="sp-open"),
    )
    assert resp.status == 200


@pytest.mark.parametrize("space_id", ["sp-invite", "sp-unknown"])
async def test_no_contact_for_a_space_that_takes_no_requests(client, space_id):
    resp = await client.post(
        f"/gfs/spaces/{space_id}/contact",
        json=_body(client.asker_seed, who="asker.home", space_id=space_id),
    )
    assert resp.status == 404


async def test_no_contact_for_the_owner_itself(client):
    resp = await client.post(
        "/gfs/spaces/sp-request/contact",
        json=_body(client.owner_seed, who="owner.home", space_id="sp-request"),
    )
    assert resp.status == 404


async def test_no_contact_when_the_owner_published_no_keywrap_key(client):
    await client.repo.upsert_instance(
        ClientInstance(
            instance_id="owner.home",
            display_name="Owner",
            public_key=client.owner_pk,
            status="active",
        )
    )
    resp = await client.post(
        "/gfs/spaces/sp-request/contact",
        json=_body(client.asker_seed, who="asker.home", space_id="sp-request"),
    )
    assert resp.status == 404


async def test_a_signature_for_another_space_is_refused(client):
    resp = await client.post(
        "/gfs/spaces/sp-request/contact",
        json=_body(client.asker_seed, who="asker.home", space_id="sp-open"),
    )
    assert resp.status == 403


async def test_an_unregistered_or_pending_caller_is_refused(client):
    stranger = generate_identity_keypair()
    resp = await client.post(
        "/gfs/spaces/sp-request/contact",
        json=_body(stranger.private_key, who="stranger.home", space_id="sp-request"),
    )
    assert resp.status == 403
    await client.repo.upsert_instance(
        ClientInstance(
            instance_id="pending.home",
            display_name="Pending",
            public_key=stranger.public_key.hex(),
            status="pending",
        )
    )
    resp = await client.post(
        "/gfs/spaces/sp-request/contact",
        json=_body(stranger.private_key, who="pending.home", space_id="sp-request"),
    )
    assert resp.status == 403


async def test_a_stale_request_is_refused(client):
    resp = await client.post(
        "/gfs/spaces/sp-request/contact",
        json=_body(
            client.asker_seed,
            who="asker.home",
            space_id="sp-request",
            ts="2020-01-01T00:00:00+00:00",
        ),
    )
    assert resp.status == 403


async def test_missing_fields_are_a_400(client):
    resp = await client.post("/gfs/spaces/sp-request/contact", json={})
    assert resp.status == 400


async def test_the_server_says_it_gives_contacts(client):
    resp = await client.get("/gfs/info")
    assert (await resp.json())["capabilities"]["space_contact"] is True
