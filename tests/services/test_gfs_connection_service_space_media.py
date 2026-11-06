"""The GFS space media cache, household side of ``GfsConnectionService``:
the signed ``space_media`` capability, the upload PUT, the anonymous fetch,
and where a space's follower copies go."""

from __future__ import annotations

from dataclasses import replace

import aiohttp

from socialhome.domain.gfs_space_media import SPACE_MEDIA_AUTH_HEADER
from socialhome.services.gfs_connection_service import GfsConnectionService

from . import test_gfs_connection_service as _base

env = _base.env

BID = "ab" * 32


class _MediaSession:
    """GET ``/gfs/info`` (scriptable) and the blob fetch; PUT recorded."""

    def __init__(
        self,
        *,
        info: dict | None = None,
        put_status: int = 201,
        blob: bytes | None = b"",
        get_status: int = 200,
        raise_on: str = "",
    ) -> None:
        self.info = info or {}
        self.put_status = put_status
        self.blob = blob
        self.get_status = get_status
        self.raise_on = raise_on
        self.gets: list[str] = []
        self.puts: list[tuple[str, bytes, dict]] = []

    def get(self, url, **_kw):
        self.gets.append(url)
        if self.raise_on == "GET":
            raise aiohttp.ClientError("down")
        if url.endswith("/gfs/info"):
            return _base._StubResp(200, self.info)
        resp = _base._StubResp(self.get_status)
        resp.content = _base._Content(self.blob or b"")
        return resp

    def put(self, url, *, data=None, headers=None, **_kw):
        if self.raise_on == "PUT":
            raise aiohttp.ClientError("down")
        self.puts.append((url, data, dict(headers or {})))
        return _base._StubResp(self.put_status, {"stored": True})

    def post(self, url, **_kw):  # pragma: no cover - unused
        return _base._StubResp(200, {})


async def _svc(env, session, *, gfs_ids=("g1",)) -> GfsConnectionService:
    _db, repo = env
    for gid in gfs_ids:
        await repo.save(
            _base._make_conn(
                gid,
                inbox_url=f"https://{gid}.example",
                public_key=_base._GFS_KP.public_key.hex(),
            )
        )
    return GfsConnectionService(repo, http_client=session, publish_client=session)


def _info(space_media) -> dict:
    caps = {"anonymous_publish": True}
    if space_media is not None:
        caps["space_media"] = space_media
    return _base._signed_info(capabilities=caps, gfs_instance_id="inst-g1")


async def test_capability_read_from_the_signed_block(env):
    session = _MediaSession(
        info=_info({"max_blob_bytes": 8_388_640, "ttl_s": 259200, "scopes": ["space"]})
    )
    svc = await _svc(env, session)
    conn = await env[1].get("g1")
    assert await svc.space_media_max_blob(conn) == 8_388_640
    # Cached: no second probe.
    probes = len(session.gets)
    assert await svc.space_media_max_blob(conn) == 8_388_640
    assert len(session.gets) == probes


async def test_no_capability_or_a_bad_shape_is_none(env):
    for bad in (None, True, {"max_blob_bytes": "x"}, {"max_blob_bytes": 0}):
        session = _MediaSession(info=_info(bad))
        svc = await _svc(env, session)
        conn = await env[1].get("g1")
        assert await svc.space_media_max_blob(conn) is None


async def test_an_unsigned_capability_is_never_trusted(env):
    info = _info({"max_blob_bytes": 1024, "ttl_s": 600})
    info["capabilities"]["space_media"] = {"max_blob_bytes": 9_999_999, "ttl_s": 1}
    session = _MediaSession(info=info)
    svc = await _svc(env, session)
    conn = await env[1].get("g1")
    assert await svc.space_media_max_blob(conn) is None


async def test_a_warm_info_fetch_records_it_and_forgetting_drops_it(env):
    session = _MediaSession()
    svc = await _svc(env, session)
    conn = await env[1].get("g1")
    svc._record_capabilities(
        conn, _info({"max_blob_bytes": 4096, "ttl_s": 600, "scopes": ["space"]})
    )
    assert svc._space_media["g1"] == (4096, frozenset({"space"}), 600)
    svc._forget_listing("g1")
    assert "g1" not in svc._space_media


async def test_put_sends_the_raw_blob_with_the_auth_header(env):
    session = _MediaSession(put_status=200)
    svc = await _svc(env, session)
    conn = await env[1].get("g1")
    status = await svc.put_space_media(
        conn,
        blob_id=BID,
        blob=b"cipher",
        auth_header="hdr",
    )
    assert status == 200
    url, data, headers = session.puts[0]
    # ONE scope-free path: the scope rides only inside the signed claim.
    assert url == f"https://g1.example/gfs/media/{BID}"
    assert data == b"cipher"
    assert headers[SPACE_MEDIA_AUTH_HEADER] == "hdr"
    assert headers["Content-Type"] == "application/octet-stream"
    # Identity-free: nothing names this household.
    assert not any(h.lower().startswith("x-sh-instance") for h in headers)


async def test_put_transport_error_is_none(env):
    session = _MediaSession(raise_on="PUT")
    svc = await _svc(env, session)
    conn = await env[1].get("g1")
    assert (
        await svc.put_space_media(conn, blob_id=BID, blob=b"x", auth_header="h") is None
    )


async def test_an_identified_put_never_goes_to_a_listing_target(env):
    session = _MediaSession()
    svc = await _svc(env, session)
    listing = replace(await env[1].get("g1"), registered=False)
    assert (
        await svc.put_space_media(
            listing,
            blob_id=BID,
            blob=b"x",
            auth_header="h",
            identified=True,
        )
        is None
    )
    assert session.puts == []


async def test_fetch_reads_the_blob(env):
    session = _MediaSession(blob=b"sealed-bytes")
    svc = await _svc(env, session)
    got = await svc.fetch_space_media(
        await env[1].get("g1"), BID, max_bytes=100, timeout_s=5
    )
    assert got == b"sealed-bytes"
    # The fetch names no scope: a channel's blob is never marked "space".
    assert session.gets[-1] == f"https://g1.example/gfs/media/{BID}"


async def test_fetch_bounds_and_misses(env):
    svc = await _svc(env, _MediaSession(blob=b"x" * 101))
    assert (
        await svc.fetch_space_media(
            await env[1].get("g1"), BID, max_bytes=100, timeout_s=5
        )
        is None
    )
    svc = await _svc(env, _MediaSession(get_status=404))
    assert (
        await svc.fetch_space_media(
            await env[1].get("g1"), BID, max_bytes=100, timeout_s=5
        )
        is None
    )
    svc = await _svc(env, _MediaSession(raise_on="GET"))
    assert (
        await svc.fetch_space_media(
            await env[1].get("g1"), BID, max_bytes=100, timeout_s=5
        )
        is None
    )
    svc = await _svc(env, _MediaSession())
    assert (
        await svc.fetch_space_media(
            await env[1].get("g1"), "nothex", max_bytes=9, timeout_s=5
        )
        is None
    )


async def test_connection_for_url_matches_the_base_url(env):
    svc = await _svc(env, _MediaSession(), gfs_ids=("g1", "g2"))
    conn = await svc.connection_for_url("https://g2.example/")
    assert conn is not None and conn.id == "g2"
    assert await svc.connection_for_url("https://elsewhere.example") is None
    assert await svc.connection_for_url("") is None


async def test_follower_targets_of_a_global_space(env):
    """Followers read only a GLOBAL space on a GFS."""
    svc = await _publishable_follower_svc(env, _MediaSession(), space_type="global")
    targets = await svc.space_media_follower_targets("sp-f")
    assert [t.id for t in targets] == ["g1"]


async def test_no_follower_targets_for_another_tier(env):
    svc = await _publishable_follower_svc(env, _MediaSession(), space_type="private")
    assert await svc.space_media_follower_targets("sp-f") == []


async def _publishable_follower_svc(env, session, *, space_type: str):
    svc = await _base._publishable_svc(
        env, session, "g1", space_id="sp-f", space_type=space_type
    )
    await env[1].publish_space("sp-f", "g1")
    return svc


async def test_the_capability_says_which_scopes_it_serves(env):
    session = _MediaSession(
        info=_info({"max_blob_bytes": 4096, "ttl_s": 600, "scopes": ["space"]})
    )
    svc = await _svc(env, session)
    conn = await env[1].get("g1")
    assert await svc.space_media_max_blob(conn, scope="space") == 4096
    assert await svc.space_media_max_blob(conn, scope="channel") is None
    both = _info({"max_blob_bytes": 4096, "ttl_s": 600, "scopes": ["channel", "space"]})
    svc = await _svc(env, _MediaSession(info=both))
    assert await svc.space_media_max_blob(conn, scope="channel") == 4096
    # A malformed scope list proves nothing.
    bad = _info({"max_blob_bytes": 4096, "ttl_s": 600, "scopes": "space"})
    svc = await _svc(env, _MediaSession(info=bad))
    assert await svc.space_media_max_blob(conn) is None


class _ChunkedContent:
    """Like aiohttp's StreamReader: ``read(n)`` returns at most one chunk."""

    def __init__(self, raw: bytes, chunk: int = 1000):
        self._raw = raw
        self._chunk = chunk

    async def read(self, n: int = -1) -> bytes:
        size = self._chunk if n < 0 else min(n, self._chunk)
        out, self._raw = self._raw[:size], self._raw[size:]
        return out


async def test_fetch_reads_a_body_that_arrives_in_chunks(env):
    """Regression (federation demo): a 1 MiB blob arrives in many chunks;
    one ``read(n)`` returned only the first, the hash check refused it."""
    blob = bytes(range(256)) * 40  # 10 240 bytes, 11 chunks of 1000
    session = _MediaSession(blob=blob)
    svc = await _svc(env, session)
    orig_get = session.get

    def _get(url, **kw):
        resp = orig_get(url, **kw)
        resp.content = _ChunkedContent(blob)
        return resp

    session.get = _get
    got = await svc.fetch_space_media(
        await env[1].get("g1"), BID, max_bytes=len(blob), timeout_s=5
    )
    assert got == blob
    session.get = lambda url, **kw: _chunked_resp(orig_get(url, **kw), blob + b"x")
    assert (
        await svc.fetch_space_media(
            await env[1].get("g1"), BID, max_bytes=len(blob), timeout_s=5
        )
        is None
    )


def _chunked_resp(resp, raw: bytes):
    resp.content = _ChunkedContent(raw)
    return resp


async def test_a_fetch_rides_the_cookie_less_session(env):
    """#1309 review E: a blob fetch is anonymous — it must not ride the
    shared session that carries this household's GFS cookies, or the GFS
    could link what a household reads to the household."""
    _db, repo = env
    await repo.save(
        _base._make_conn(
            "g1",
            inbox_url="https://g1.example",
            public_key=_base._GFS_KP.public_key.hex(),
        )
    )
    identified = _MediaSession(blob=b"never")
    anonymous = _MediaSession(blob=b"sealed")
    svc = GfsConnectionService(repo, http_client=identified, publish_client=anonymous)
    got = await svc.fetch_space_media(
        await repo.get("g1"), BID, max_bytes=100, timeout_s=5
    )
    assert got == b"sealed"
    assert identified.gets == []
    assert anonymous.gets == [f"https://g1.example/gfs/media/{BID}"]


async def test_no_session_means_no_fetch(env):
    _db, repo = env
    await repo.save(_base._make_conn("g1", inbox_url="https://g1.example"))
    svc = GfsConnectionService(repo, http_client=_MediaSession(), publish_client=None)
    assert (
        await svc.fetch_space_media(await repo.get("g1"), BID, max_bytes=9, timeout_s=5)
        is None
    )


async def test_the_capability_carries_the_servers_ttl(env):
    session = _MediaSession(
        info=_info({"max_blob_bytes": 4096, "ttl_s": 600, "scopes": ["space"]})
    )
    svc = await _svc(env, session)
    conn = await env[1].get("g1")
    assert await svc.space_media_ttl_s(conn) == 600
    bad = _info({"max_blob_bytes": 4096, "ttl_s": "x", "scopes": ["space"]})
    svc = await _svc(env, _MediaSession(info=bad))
    assert await svc.space_media_ttl_s(conn) is None
