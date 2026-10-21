"""``POST /api/moments/{id}/share-link`` — the author's per-moment
share link for a public moment."""

from __future__ import annotations

import pytest

from socialhome.app_keys import moment_public_share_service_key
from socialhome.services.moment_public_share_service import ShareLinkUnavailableError
from socialhome.services.moment_service import MomentNotFoundError

from .conftest import _auth, client_with_stubs


class _Stub:
    def __init__(self) -> None:
        self.calls: list = []
        self.raises: Exception | None = None

    async def link(self, *, user_id, moment_id, gfs_id=None):
        self.calls.append((user_id, moment_id, gfs_id))
        if self.raises is not None:
            raise self.raises
        return "https://viewer.social-home.io/v/0123456789abcdef/#m=" + moment_id


@pytest.fixture
def stub():
    return _Stub()


@pytest.fixture
async def client(request, make_client):
    return await client_with_stubs(
        request, make_client, {"stub": moment_public_share_service_key}
    )


async def test_the_author_gets_the_link(client, stub):
    r = await client.post(
        "/api/moments/m-1/share-link", json={"gfs_id": "g1"}, headers=_auth(client._tok)
    )
    assert r.status == 200
    assert (await r.json())["url"].endswith("#m=m-1")
    assert stub.calls == [(client._uid, "m-1", "g1")]


async def test_no_body_picks_a_gfs(client, stub):
    r = await client.post("/api/moments/m-1/share-link", headers=_auth(client._tok))
    assert r.status == 200
    assert stub.calls[0][2] is None


async def test_not_the_authors_moment_is_a_404(client, stub):
    stub.raises = MomentNotFoundError("m-1")
    r = await client.post("/api/moments/m-1/share-link", headers=_auth(client._tok))
    assert r.status == 404


async def test_no_viewer_or_gfs_is_a_409_with_a_code(client, stub):
    stub.raises = ShareLinkUnavailableError()
    r = await client.post("/api/moments/m-1/share-link", headers=_auth(client._tok))
    assert r.status == 409
    assert (await r.json())["error"]["code"] == "SHARE_LINK_UNAVAILABLE"


async def test_a_bad_gfs_id_is_a_400(client, stub):
    r = await client.post(
        "/api/moments/m-1/share-link", json={"gfs_id": 5}, headers=_auth(client._tok)
    )
    assert r.status == 400
    assert stub.calls == []


async def test_anonymous_callers_get_nothing(client, stub):
    r = await client.post("/api/moments/m-1/share-link")
    assert r.status == 401


async def test_the_real_service_refuses_an_unknown_moment(make_client):
    client = await make_client()
    r = await client.post("/api/moments/nope/share-link", headers=_auth(client._tok))
    assert r.status == 404
