"""Tests for ``GET /gfs/spaces/{space_id}/subscribers`` (Phase-5b-c reconcile).

A seed-holder (owner OR delegated admin) pulls the subscriber list to re-seal
the per-space content key — making delivery work owner-offline and catching
notifies missed while no seed-holder was online. The endpoint is
SECURITY-SENSITIVE: it exposes the subscriber list, so it is gated on a
SPACE-AUTHORITY signature (proving the caller holds the space seed pinned at
publish time) plus a ±300 s replay guard.

Invariants under test:

* a VALID space-authority signature returns the subscriber list including each
  subscriber's key-wrap material;
* NO / FORGED / STALE signature → 403;
* an unknown space (no pinned pubkey) → 403;
* the JOIN surfaces empty key-wrap fields for a subscriber that registered
  without a key-wrap key.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
    sign_authority_event,
)
from socialhome.crypto import (
    generate_identity_keypair,
    generate_space_keypair,
)
from socialhome.global_server import create_gfs_app
from socialhome.global_server.app_keys import gfs_fed_repo_key
from socialhome.global_server.domain import ClientInstance, GlobalSpace


@pytest.fixture
async def gfs_client(tmp_path):
    app = create_gfs_app(db_path=tmp_path / "gfs.db")
    async with TestClient(TestServer(app)) as tc:
        yield tc


async def _seed_space(app, *, space_id="sp", with_pubkey=True):
    """Publish a space row (TOFU-pinning a space authority pubkey) and return
    the space keypair so the test can sign queries with the seed."""
    skp = generate_space_keypair()
    fed_repo = app[gfs_fed_repo_key]
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="owner.home",
            display_name="Owner",
            public_key="aa" * 32,
            status="active",
        )
    )
    await fed_repo.upsert_space(
        GlobalSpace(
            space_id=space_id,
            owning_instance="owner.home",
            name="S",
            status="active",
            identity_public_key=skp.public_key.hex() if with_pubkey else "",
        )
    )
    return skp


async def _add_subscriber(app, *, space_id, instance_id, with_keywrap):
    fed_repo = app[gfs_fed_repo_key]
    kp = generate_identity_keypair()
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id=instance_id,
            display_name=instance_id,
            public_key=kp.public_key.hex(),
            status="active",
            keywrap_public_key=("cc" * 32) if with_keywrap else "",
            kem_suite="x25519" if with_keywrap else "",
            keywrap_sig="sig" if with_keywrap else "",
        )
    )
    await fed_repo.add_subscriber(space_id=space_id, instance_id=instance_id)


def _signed_query(skp, *, space_id, ts=None) -> dict[str, str]:
    """Return the query params for a space-authority-signed subscribers query."""
    ts = ts or datetime.now(timezone.utc).isoformat()
    sig = sign_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
        space_id=space_id,
        payload={"space_id": space_id, "ts": ts},
        space_seed=skp.private_key,
    )
    return {
        "ts": ts,
        "authority_sig": sig["authority_sig"],
        "authority_sig_suite": sig["authority_sig_suite"],
    }


async def test_valid_authority_sig_returns_subscribers_with_keys(gfs_client):
    app = gfs_client.server.app
    skp = await _seed_space(app, space_id="sp")
    await _add_subscriber(app, space_id="sp", instance_id="sub-kw", with_keywrap=True)
    await _add_subscriber(
        app, space_id="sp", instance_id="sub-bare", with_keywrap=False
    )

    resp = await gfs_client.get(
        "/gfs/spaces/sp/subscribers", params=_signed_query(skp, space_id="sp")
    )
    assert resp.status == 200
    subs = (await resp.json())["subscribers"]
    by_id = {s["instance_id"]: s for s in subs}
    assert set(by_id) == {"sub-kw", "sub-bare"}
    assert by_id["sub-kw"]["keywrap_public_key"] == "cc" * 32
    assert by_id["sub-kw"]["keywrap_sig"] == "sig"
    assert "identity_public_key" in by_id["sub-kw"]
    # A subscriber registered without a key-wrap key surfaces empty fields.
    assert by_id["sub-bare"]["keywrap_public_key"] == ""
    assert by_id["sub-bare"]["keywrap_sig"] == ""


async def test_missing_signature_is_403(gfs_client):
    app = gfs_client.server.app
    await _seed_space(app, space_id="sp")
    resp = await gfs_client.get("/gfs/spaces/sp/subscribers")
    assert resp.status == 403


async def test_forged_signature_is_403(gfs_client):
    """A signature made with the WRONG seed (not the pinned space key) is
    rejected — a registered peer that knows the space_id can't enumerate
    subscribers."""
    app = gfs_client.server.app
    await _seed_space(app, space_id="sp")
    attacker = generate_space_keypair()
    resp = await gfs_client.get(
        "/gfs/spaces/sp/subscribers",
        params=_signed_query(attacker, space_id="sp"),
    )
    assert resp.status == 403


async def test_stale_timestamp_is_403(gfs_client):
    app = gfs_client.server.app
    skp = await _seed_space(app, space_id="sp")
    stale = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    resp = await gfs_client.get(
        "/gfs/spaces/sp/subscribers",
        params=_signed_query(skp, space_id="sp", ts=stale),
    )
    assert resp.status == 403


async def test_unknown_space_is_403(gfs_client):
    skp = generate_space_keypair()
    resp = await gfs_client.get(
        "/gfs/spaces/never-published/subscribers",
        params=_signed_query(skp, space_id="never-published"),
    )
    assert resp.status == 403


async def test_space_without_pinned_pubkey_is_403(gfs_client):
    """A space published before authority pinning has no key to verify the
    query against → fail-closed 403."""
    app = gfs_client.server.app
    skp = await _seed_space(app, space_id="sp-nopin", with_pubkey=False)
    resp = await gfs_client.get(
        "/gfs/spaces/sp-nopin/subscribers",
        params=_signed_query(skp, space_id="sp-nopin"),
    )
    assert resp.status == 403


async def test_signature_for_other_space_is_403(gfs_client):
    """A query signed over a DIFFERENT space_id can't be replayed onto this
    space (the signing bytes bind the space id)."""
    app = gfs_client.server.app
    skp = await _seed_space(app, space_id="sp")
    # Sign for "other" but hit "sp".
    params = _signed_query(skp, space_id="other")
    resp = await gfs_client.get("/gfs/spaces/sp/subscribers", params=params)
    assert resp.status == 403
