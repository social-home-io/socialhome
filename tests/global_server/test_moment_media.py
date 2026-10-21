"""GFS: encrypted public-moment media blobs (v_63) — stored once,
content-addressed, fetched by followers and share-link viewers; ciphertext
only, bounded, expiring."""

from __future__ import annotations

import json
import os
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import b64url_encode, generate_identity_keypair, sign_ed25519
from socialhome.global_server import moment_media as gmm
from socialhome.global_server.app_keys import (
    gfs_fed_repo_key,
    gfs_moment_media_store_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app
from socialhome.moment_public_media import MAX_BLOB_BYTES, blob_id_of, seal_media

ITEM = os.urandom(32)


@pytest.fixture
async def client(tmp_dir):
    app = create_gfs_app(
        GfsConfig(
            host="127.0.0.1",
            port=0,
            base_url="http://gfs.test",
            data_dir=str(tmp_dir),
            instance_id="gfs-test",
        )
    )
    async with TestClient(TestServer(app)) as tc:
        tc._app = app
        yield tc


def _sign(kp, body: dict) -> str:
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return b64url_encode(sign_ed25519(kp.private_key, canonical))


async def _instance(client, iid: str):
    kp = generate_identity_keypair()
    await client._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id=iid,
            display_name=iid,
            public_key=kp.public_key.hex(),
            status="active",
        )
    )
    return kp


@pytest.fixture
async def author(client):
    kp = await _instance(client, "inst-author")
    body = {
        "user_id": "u-author",
        "instance_id": "inst-author",
        "username": "alice",
        "display_name": "Alice",
        "home_instance_pk": kp.public_key.hex(),
    }
    r = await client.post(
        "/gfs/moments/users/register", json={**body, "signature": _sign(kp, body)}
    )
    assert r.status == 201
    return kp


def _blob(data: bytes = b"\x89PNG" + b"x" * 1000) -> bytes:
    return seal_media(
        item_key=ITEM,
        author_user_id="u-author",
        moment_id="m1",
        role="media",
        data=data,
    )


def _headers(kp, blob_id: str, *, iid="inst-author", author="u-author", **over):
    expires = over.pop("expires", int(time.time()) + 86_400)
    ts = over.pop("ts", int(time.time()))
    body = {
        "instance_id": iid,
        "blob_id": blob_id,
        "author_user_id": author,
        "expires_at": expires,
        "ts": ts,
    }
    h = {
        "X-SH-Instance": iid,
        "X-SH-Author": author,
        "X-SH-Expires": str(expires),
        "X-SH-Timestamp": str(ts),
        "X-SH-Signature": _sign(kp, body),
        "Content-Type": "application/octet-stream",
    }
    h.update(over)
    return h


async def test_upload_once_fetch_anywhere(client, author):
    blob = _blob()
    bid = blob_id_of(blob)
    r = await client.put(
        f"/gfs/moments/media/{bid}", data=blob, headers=_headers(author, bid)
    )
    assert r.status == 201
    # Re-uploading the same bytes (another retry) is a no-op.
    r = await client.put(
        f"/gfs/moments/media/{bid}", data=blob, headers=_headers(author, bid)
    )
    assert r.status == 200
    g = await client.get(f"/gfs/moments/media/{bid}")
    assert g.status == 200
    assert await g.read() == blob
    assert g.headers["Access-Control-Allow-Origin"] == "*"
    assert "immutable" in g.headers["Cache-Control"]
    assert g.headers["Content-Type"] == "application/octet-stream"
    assert g.headers["X-Content-Type-Options"] == "nosniff"


async def test_the_body_must_match_its_address(client, author):
    blob = _blob()
    wrong = blob_id_of(b"other")
    r = await client.put(
        f"/gfs/moments/media/{wrong}", data=blob, headers=_headers(author, wrong)
    )
    assert r.status == 422
    assert (await client.get(f"/gfs/moments/media/{wrong}")).status == 404


async def test_only_the_authors_household_may_upload(client, author):
    other = await _instance(client, "inst-other")
    blob = _blob()
    bid = blob_id_of(blob)
    r = await client.put(
        f"/gfs/moments/media/{bid}",
        data=blob,
        headers=_headers(other, bid, iid="inst-other"),
    )
    assert r.status == 403
    forged = _headers(author, bid)
    forged["X-SH-Signature"] = _headers(other, bid)["X-SH-Signature"]
    r = await client.put(f"/gfs/moments/media/{bid}", data=blob, headers=forged)
    assert r.status == 401


@pytest.mark.parametrize(
    "over",
    [
        {"ts": 1},  # stale signature
        {"expires": 1},  # already expired
        {"expires": 4_000_000_000},  # beyond what a moment can live
    ],
)
async def test_stale_or_out_of_range_uploads_are_refused(client, author, over):
    blob = _blob()
    bid = blob_id_of(blob)
    r = await client.put(
        f"/gfs/moments/media/{bid}", data=blob, headers=_headers(author, bid, **over)
    )
    assert r.status in (401, 422)


async def test_a_blob_that_is_no_padded_bucket_is_refused(client, author):
    blob = os.urandom(1000)  # not a bucket size
    bid = blob_id_of(blob)
    r = await client.put(
        f"/gfs/moments/media/{bid}", data=blob, headers=_headers(author, bid)
    )
    assert r.status == 422


async def test_an_oversized_body_is_refused(client, author):
    blob = b"\0" * (MAX_BLOB_BYTES + 1)
    bid = blob_id_of(blob)
    r = await client.put(
        f"/gfs/moments/media/{bid}", data=blob, headers=_headers(author, bid)
    )
    assert r.status == 413


async def test_an_authors_storage_is_capped(client, author, monkeypatch):
    monkeypatch.setattr(gmm, "MAX_AUTHOR_LIVE_BYTES", 100_000)
    first = _blob()
    r = await client.put(
        f"/gfs/moments/media/{blob_id_of(first)}",
        data=first,
        headers=_headers(author, blob_id_of(first)),
    )
    assert r.status == 201
    second = _blob(b"y" * 1000)
    r = await client.put(
        f"/gfs/moments/media/{blob_id_of(second)}",
        data=second,
        headers=_headers(author, blob_id_of(second)),
    )
    assert r.status == 429


async def test_uploads_per_hour_are_capped(client, author, monkeypatch):
    monkeypatch.setattr(gmm, "MAX_UPLOADS_PER_HOUR", 1)
    for i, expected in ((0, 201), (1, 429)):
        blob = _blob(bytes([i]) * 100)
        r = await client.put(
            f"/gfs/moments/media/{blob_id_of(blob)}",
            data=blob,
            headers=_headers(author, blob_id_of(blob)),
        )
        assert r.status == expected


async def test_the_author_deletes_and_expiry_sweeps(client, author):
    store = client._app[gfs_moment_media_store_key]
    blob = _blob()
    bid = blob_id_of(blob)
    await client.put(
        f"/gfs/moments/media/{bid}", data=blob, headers=_headers(author, bid)
    )
    body = {"instance_id": "inst-author", "author_user_id": "u-author", "blob_id": bid}
    r = await client.post(
        f"/gfs/moments/media/{bid}/delete",
        json={**body, "signature": _sign(author, body)},
    )
    assert r.status == 200 and (await r.json()) == {"deleted": True}
    assert (await client.get(f"/gfs/moments/media/{bid}")).status == 404
    # Expiry: a blob past its moment's life goes on the next sweep.
    soon = int(time.time()) + 60
    blob2 = _blob(b"z" * 10)
    bid2 = blob_id_of(blob2)
    await client.put(
        f"/gfs/moments/media/{bid2}",
        data=blob2,
        headers=_headers(author, bid2, expires=soon),
    )
    assert await store.prune_expired(soon + 1) == 1
    assert (await client.get(f"/gfs/moments/media/{bid2}")).status == 404


async def test_another_household_cannot_delete(client, author):
    other = await _instance(client, "inst-other")
    blob = _blob()
    bid = blob_id_of(blob)
    await client.put(
        f"/gfs/moments/media/{bid}", data=blob, headers=_headers(author, bid)
    )
    body = {"instance_id": "inst-other", "author_user_id": "u-author", "blob_id": bid}
    r = await client.post(
        f"/gfs/moments/media/{bid}/delete",
        json={**body, "signature": _sign(other, body)},
    )
    assert r.status == 403
    assert (await client.get(f"/gfs/moments/media/{bid}")).status == 200


async def test_deregistering_drops_the_authors_blobs(client, author):
    blob = _blob()
    bid = blob_id_of(blob)
    await client.put(
        f"/gfs/moments/media/{bid}", data=blob, headers=_headers(author, bid)
    )
    body = {"user_id": "u-author", "instance_id": "inst-author"}
    r = await client.post(
        "/gfs/moments/users/u-author/deregister",
        json={**body, "signature": _sign(author, body)},
    )
    assert r.status == 200
    assert (await client.get(f"/gfs/moments/media/{bid}")).status == 404


async def test_a_bad_address_is_a_plain_404(client):
    for path in ("not-hex", "A" * 64, "0" * 64):
        assert (await client.get(f"/gfs/moments/media/{path}")).status == 404


async def test_info_advertises_encrypted_moment_media(client):
    info = await (await client.get("/gfs/info")).json()
    assert info["capabilities"]["encrypted_moment_media"] is True
