"""Coverage fill for :class:`GfsConnectionService` — pair, publish,
unpublish, appeal, and the _client() guard.
"""

from __future__ import annotations

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.services.gfs_connection_service import (
    GfsConnectionError,
    GfsConnectionService,
)


# ─── stubs (same shape as test_gfs_connection_service) ──────────────────


class _StubResp:
    __slots__ = ("status", "_body", "_text")

    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status = status
        self._body = body or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return self._text


class _RaisingSession:
    """Session whose methods raise aiohttp.ClientError."""

    def __init__(self) -> None:
        import aiohttp

        self.exc = aiohttp.ClientError("boom")

    def get(self, *a, **kw):
        raise self.exc

    def post(self, *a, **kw):
        raise self.exc

    def delete(self, *a, **kw):
        raise self.exc


class _StubSession:
    def __init__(
        self,
        *,
        status: int = 200,
        body: dict | None = None,
        method_responses: dict[str, tuple[int, dict]] | None = None,
    ):
        self._status = status
        self._body = body or {}
        self.method_responses = method_responses or {}
        self.calls: list[tuple[str, str]] = []

    def _resp(self, method: str) -> _StubResp:
        override = self.method_responses.get(method)
        if override is not None:
            status, body = override
            return _StubResp(status, body)
        return _StubResp(self._status, self._body)

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return self._resp("GET")

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        return self._resp("POST")

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url))
        return self._resp("DELETE")


_OWN_PAIR_KW = {
    "own_instance_id": "alpha.home",
    "own_public_key_hex": "aa" * 32,
    "own_inbox_url": "https://alpha.example/federation/inbox",
    "own_display_name": "Alpha House",
}


def _info_response(public_key: str = "bb" * 32) -> tuple[int, dict]:
    return (
        200,
        {
            "gfs_instance_id": "inst-1",
            "public_key": public_key,
            "server_name": "Demo",
            "base_url": "https://gfs.example",
        },
    )


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )
    yield SqliteGfsConnectionRepo(db)
    await db.shutdown()


def _conn(gfs_id: str = "gfs-1", status: str = "active") -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"inst-{gfs_id}",
        display_name=gfs_id,
        public_key="pk",
        inbox_url="https://gfs.example",
        status=status,
        paired_at="2025-01-01T00:00:00+00:00",
    )


class _StubSpaceRepo:
    """Minimal space repo whose ``get`` returns a publishable space.

    The GFS now mandates a signed publish body, so ``publish_space`` needs
    a signing identity + a local space row; this stub supplies the row.
    """

    def __init__(self, space_id: str) -> None:
        self._space_id = space_id

    async def get(self, space_id: str):
        from socialhome.domain.space import (
            JoinMode,
            Space,
            SpaceFeatures,
            SpaceType,
        )

        if space_id != self._space_id:
            return None
        return Space(
            id=space_id,
            name="Pubble",
            owner_instance_id="alpha.home",
            owner_username="alice",
            identity_public_key="aa" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.OPEN,
        )


def _wire_publish_ctx(svc, space_id: str) -> None:
    kp = generate_identity_keypair()
    svc.attach_publish_context(
        space_repo=_StubSpaceRepo(space_id),
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )


# ─── _client guard ──────────────────────────────────────────────────────


async def test_client_without_session_raises(env):
    svc = GfsConnectionService(env)
    with pytest.raises(RuntimeError):
        svc._client()


async def test_attach_session_once(env):
    svc = GfsConnectionService(env)

    class _Sess:
        pass

    s = _Sess()
    svc.attach_session(s)  # type: ignore[arg-type]
    assert svc._client() is s
    # Attach again is ignored.
    svc.attach_session(_Sess())  # type: ignore[arg-type]
    assert svc._client() is s


# ─── pair ───────────────────────────────────────────────────────────────


async def test_pair_missing_fields_raises(env):
    svc = GfsConnectionService(env, http_client=_StubSession())
    with pytest.raises(GfsConnectionError):
        await svc.pair({}, **_OWN_PAIR_KW)


async def test_pair_registers_and_saves(env):
    session = _StubSession(
        method_responses={
            "GET": _info_response(),
            "POST": (200, {"status": "registered"}),
        },
    )
    svc = GfsConnectionService(env, http_client=session)
    conn = await svc.pair(
        {"gfs_url": "https://gfs.example/", "token": "tkn"},
        **_OWN_PAIR_KW,
    )
    assert conn.gfs_instance_id == "inst-1"
    assert conn.display_name == "Demo"
    assert conn.inbox_url == "https://gfs.example"


async def test_pair_non_200_raises(env):
    session = _StubSession(
        method_responses={"GET": _info_response(), "POST": (401, {})},
    )
    svc = GfsConnectionService(env, http_client=session)
    with pytest.raises(GfsConnectionError):
        await svc.pair(
            {"gfs_url": "https://x", "token": "tkn"},
            **_OWN_PAIR_KW,
        )


async def test_pair_network_error_raises(env):
    svc = GfsConnectionService(env, http_client=_RaisingSession())
    with pytest.raises(GfsConnectionError):
        await svc.pair(
            {"gfs_url": "https://x", "token": "t"},
            **_OWN_PAIR_KW,
        )


async def test_pair_missing_gfs_info_fields_raises(env):
    session = _StubSession(
        method_responses={"GET": (200, {})},
    )
    svc = GfsConnectionService(env, http_client=session)
    with pytest.raises(GfsConnectionError):
        await svc.pair(
            {"gfs_url": "https://x", "token": "t"},
            **_OWN_PAIR_KW,
        )


# ─── disconnect ─────────────────────────────────────────────────────────


async def test_disconnect_unknown_raises(env):
    svc = GfsConnectionService(env)
    with pytest.raises(GfsConnectionError):
        await svc.disconnect("nope")


async def test_disconnect_removes(env):
    await env.save(_conn("g1"))
    svc = GfsConnectionService(env)
    await svc.disconnect("g1")
    assert await env.get("g1") is None


async def test_list_connections_empty(env):
    svc = GfsConnectionService(env)
    assert await svc.list_connections() == []


# ─── publish_space / unpublish_space ────────────────────────────────────


async def test_publish_space_unknown_gfs_raises(env):
    svc = GfsConnectionService(env, http_client=_StubSession())
    with pytest.raises(GfsConnectionError):
        await svc.publish_space("sp1", "gfs-missing")


async def test_publish_space_http_error_raises_and_skips_local(env):
    await env.save(_conn("g1"))
    session = _StubSession(status=500)
    svc = GfsConnectionService(env, http_client=session)
    with pytest.raises(GfsConnectionError):
        await svc.publish_space("sp1", "g1")
    assert await env.list_publications_for_space("sp1") == []


async def test_publish_space_network_error_raises_and_skips_local(env):
    await env.save(_conn("g1"))
    svc = GfsConnectionService(env, http_client=_RaisingSession())
    with pytest.raises(GfsConnectionError):
        await svc.publish_space("sp1", "g1")
    assert await env.list_publications_for_space("sp1") == []


async def test_publish_space_success(env):
    await env.save(_conn("g1"))
    session = _StubSession(status=200)
    svc = GfsConnectionService(env, http_client=session)
    _wire_publish_ctx(svc, "sp1")
    pub = await svc.publish_space("sp1", "g1")
    assert session.calls[0][0] == "POST"
    assert session.calls[0][1].endswith("/gfs/spaces/sp1/publish")
    assert pub.space_id == "sp1"


async def test_unpublish_space_unknown_gfs_raises(env):
    svc = GfsConnectionService(env, http_client=_StubSession())
    _wire_publish_ctx(svc, "sp1")
    with pytest.raises(GfsConnectionError):
        await svc.unpublish_space("sp1", "gfs-missing")


async def test_unpublish_space_success(env):
    await env.save(_conn("g1"))
    session = _StubSession(status=204)
    svc = GfsConnectionService(env, http_client=session)
    _wire_publish_ctx(svc, "sp1")
    await svc.unpublish_space("sp1", "g1")
    assert session.calls[0][0] == "POST"


async def test_unpublish_space_http_error_raises(env):
    await env.save(_conn("g1"))
    await env.publish_space("sp1", "g1")
    session = _StubSession(status=500)
    svc = GfsConnectionService(env, http_client=session)
    _wire_publish_ctx(svc, "sp1")
    with pytest.raises(GfsConnectionError):
        await svc.unpublish_space("sp1", "g1")
    # Local row preserved — the GFS still believes it's published.
    assert len(await env.list_publications_for_space("sp1")) == 1


async def test_unpublish_space_network_error_raises(env):
    await env.save(_conn("g1"))
    svc = GfsConnectionService(env, http_client=_RaisingSession())
    _wire_publish_ctx(svc, "sp1")
    with pytest.raises(GfsConnectionError):
        await svc.unpublish_space("sp1", "g1")


async def test_unpublish_space_404_is_success(env):
    await env.save(_conn("g1"))
    await env.publish_space("sp1", "g1")
    session = _StubSession(status=404)
    svc = GfsConnectionService(env, http_client=session)
    _wire_publish_ctx(svc, "sp1")
    await svc.unpublish_space("sp1", "g1")
    assert await env.list_publications_for_space("sp1") == []


# ─── publish_space_to_all / unpublish_space_from_all ────────────────────


async def test_publish_space_to_all(env):
    await env.save(_conn("g1"))
    await env.save(_conn("g2"))
    svc = GfsConnectionService(env, http_client=_StubSession(status=200))
    _wire_publish_ctx(svc, "sp1")
    n = await svc.publish_space_to_all("sp1")
    assert n == 2


async def test_unpublish_space_from_all(env):
    await env.save(_conn("g1"))
    await env.save(_conn("g2"))
    svc = GfsConnectionService(env, http_client=_StubSession(status=204))
    _wire_publish_ctx(svc, "sp1")
    n = await svc.unpublish_space_from_all("sp1")
    assert n == 2


async def test_publish_space_to_all_individual_failure_logs(env):
    """One failing GFS doesn't abort the fan-out."""
    await env.save(_conn("g-ok"))
    await env.save(_conn("g-bad"))

    class _Mixed:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, url, **kw):
            self.calls += 1
            # Second call raises.
            if self.calls > 1:
                import aiohttp

                raise aiohttp.ClientError("second fails")
            return _StubResp(200)

    svc = GfsConnectionService(env, http_client=_Mixed())
    _wire_publish_ctx(svc, "sp1")
    n = await svc.publish_space_to_all("sp1")
    # One GFS succeeded, the other raised + was caught: only the
    # success is counted, and the fan-out did not abort.
    assert n == 1


# ─── send_appeal ────────────────────────────────────────────────────────


async def test_send_appeal_unknown_gfs_returns_false(env):
    svc = GfsConnectionService(env)
    ok = await svc.send_appeal(
        "bogus",
        target_type="space",
        target_id="sp",
        message="pls",
        from_instance="me.home",
        signing_key=b"\x00" * 32,
    )
    assert ok is False


async def test_send_appeal_inactive_conn_returns_false(env):
    await env.save(_conn("g1", status="suspended"))
    svc = GfsConnectionService(env)
    ok = await svc.send_appeal(
        "g1",
        target_type="space",
        target_id="sp",
        message="x",
        from_instance="me",
        signing_key=b"\x00" * 32,
    )
    assert ok is False


async def test_send_appeal_without_session_returns_false(env):
    await env.save(_conn("g1"))
    svc = GfsConnectionService(env)  # no http_client
    ok = await svc.send_appeal(
        "g1",
        target_type="space",
        target_id="sp",
        message="x",
        from_instance="me",
        signing_key=b"\x00" * 32,
    )
    assert ok is False


async def test_send_appeal_200_returns_true(env):
    await env.save(_conn("g1"))
    session = _StubSession(status=200)
    svc = GfsConnectionService(env, http_client=session)
    ok = await svc.send_appeal(
        "g1",
        target_type="space",
        target_id="sp",
        message="x",
        from_instance="me",
        signing_key=b"\x00" * 32,
    )
    assert ok is True
    assert session.calls[0][1].endswith("/gfs/appeal")


async def test_send_appeal_non_2xx_returns_false(env):
    await env.save(_conn("g1"))
    session = _StubSession(status=400)
    svc = GfsConnectionService(env, http_client=session)
    ok = await svc.send_appeal(
        "g1",
        target_type="space",
        target_id="sp",
        message="x",
        from_instance="me",
        signing_key=b"\x00" * 32,
    )
    assert ok is False


async def test_send_appeal_network_error_returns_false(env):
    await env.save(_conn("g1"))
    svc = GfsConnectionService(env, http_client=_RaisingSession())
    ok = await svc.send_appeal(
        "g1",
        target_type="space",
        target_id="sp",
        message="x",
        from_instance="me",
        signing_key=b"\x00" * 32,
    )
    assert ok is False


# ─── report_fraud edge: inactive conn ──────────────────────────────────


async def test_report_fraud_inactive_returns_false(env):
    await env.save(_conn("g1", status="suspended"))
    svc = GfsConnectionService(env, http_client=_StubSession())
    ok = await svc.report_fraud(
        "g1",
        target_type="space",
        target_id="sp",
        category="spam",
        notes=None,
        reporter_instance_id="me",
        reporter_user_id=None,
        signing_key=b"\x00" * 32,
    )
    assert ok is False


async def test_report_fraud_without_session_returns_false(env):
    await env.save(_conn("g1"))
    svc = GfsConnectionService(env)  # no http_client
    ok = await svc.report_fraud(
        "g1",
        target_type="space",
        target_id="sp",
        category="spam",
        notes=None,
        reporter_instance_id="me",
        reporter_user_id=None,
        signing_key=b"\x00" * 32,
    )
    assert ok is False


async def test_report_fraud_network_error_returns_false(env):
    await env.save(_conn("g1"))
    svc = GfsConnectionService(env, http_client=_RaisingSession())
    ok = await svc.report_fraud(
        "g1",
        target_type="space",
        target_id="sp",
        category="spam",
        notes=None,
        reporter_instance_id="me",
        reporter_user_id=None,
        signing_key=b"\x00" * 32,
    )
    assert ok is False
