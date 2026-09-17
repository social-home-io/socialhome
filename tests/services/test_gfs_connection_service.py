"""Tests for GfsConnectionService + SqliteGfsConnectionRepo."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from socialhome.crypto import (
    b64url_decode,
    derive_instance_id,
    generate_identity_keypair,
    verify_ed25519,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.services.gfs_connection_service import (
    GfsConnectionError,
    GfsConnectionService,
)


# ─── Helpers ────────────────────────────────────────────────────────────


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


class _StubSession:
    """Stub aiohttp session.

    Per-method overrides land in ``method_responses`` (keyed by ``"GET"``
    / ``"POST"`` / etc.); fall back to the default ``status`` / ``body``
    when the method has no override.
    """

    __slots__ = ("_status", "_body", "method_responses", "calls", "_last_body")

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
        # Last JSON body the caller passed via ``json=`` — exposed for
        # tests that need to assert what got serialized on the wire
        # (e.g. publish-body signature verification).
        self._last_body: dict | None = None

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
        if "json" in kw:
            self._last_body = kw["json"]
        return self._resp("POST")

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url))
        if "json" in kw:
            self._last_body = kw["json"]
        return self._resp("DELETE")


# ─── Fixtures ───────────────────────────────────────────────────────────


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
    repo = SqliteGfsConnectionRepo(db)
    yield db, repo
    await db.shutdown()


def _make_conn(
    gfs_id: str = "gfs-1",
    *,
    status: str = "active",
    inbox_url: str = "https://gfs.example.com",
) -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"inst-{gfs_id}",
        display_name=f"GFS {gfs_id}",
        public_key="pubkey-hex",
        inbox_url=inbox_url,
        status=status,
        paired_at="2025-01-01T00:00:00+00:00",
    )


async def _publishable_svc(env, session, gfs_id: str, *, space_id: str):
    """Build a GfsConnectionService with the publish context wired + a real
    local space row so ``publish_space`` can compose a signed body.

    The GFS now mandates a signature on every publish, so the service must
    always have a signing identity + a space to describe; this helper sets
    both up for the publish-path tests.
    """
    from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn(gfs_id))
    space_repo = SqliteSpaceRepo(db)
    await space_repo.save(
        Space(
            id=space_id,
            name="Publishable",
            owner_instance_id="alpha.home",
            owner_username="alice",
            identity_public_key="aa" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.OPEN,
        )
    )
    kp = generate_identity_keypair()
    svc = GfsConnectionService(conn_repo, http_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    return svc


def _signing_svc(repo, session) -> tuple[GfsConnectionService, bytes]:
    """A service with only the signing identity wired (no space repo).

    ``unpublish_space`` signs its body with the household identity key, so
    every unpublish test needs an identity; the space metadata is irrelevant
    there (the GFS looks the row up by id). Returns the service plus the
    public key so a test can verify the signature it produced.
    """
    kp = generate_identity_keypair()
    svc = GfsConnectionService(repo, http_client=session)
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    return svc, kp.public_key


# ─── Repo tests ─────────────────────────────────────────────────────────


async def test_save_and_get(env):
    _, repo = env
    conn = _make_conn("gfs-1")
    await repo.save(conn)
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.id == "gfs-1"
    assert got.gfs_instance_id == "inst-gfs-1"


async def test_get_nonexistent_returns_none(env):
    _, repo = env
    assert await repo.get("nope") is None


async def test_list_active_filters_status(env):
    _, repo = env
    await repo.save(_make_conn("a1", status="active"))
    await repo.save(_make_conn("a2", status="suspended"))
    await repo.save(_make_conn("a3", status="pending"))
    active = await repo.list_active()
    assert len(active) == 1
    assert active[0].id == "a1"


# ── Service: refresh_connection_metadata ───────────────────────────────


async def test_refresh_connection_metadata_updates_changed_name(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1", status="active", inbox_url="https://gfs.test"))
    session = _StubSession(status=200, body={"server_name": "New Name"})
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    await svc.refresh_connection_metadata("gfs-1")
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.display_name == "New Name"
    # Hit GET /gfs/info on the connection's inbox URL.
    assert session.calls and session.calls[0][0] == "GET"
    assert session.calls[0][1].endswith("/gfs/info")


async def test_refresh_connection_metadata_noop_when_unchanged(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1", status="active", inbox_url="https://gfs.test"))
    # The fake GFS returns the SAME name the row already holds.
    session = _StubSession(status=200, body={"server_name": "GFS gfs-1"})
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    await svc.refresh_connection_metadata("gfs-1")
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.display_name == "GFS gfs-1"


async def test_refresh_connection_metadata_swallows_transport_error(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1", status="active", inbox_url="https://gfs.test"))

    class _BoomSession:
        def get(self, url, **kw):
            raise aiohttp.ClientError("boom")

    svc = GfsConnectionService(repo, http_client=_BoomSession())  # type: ignore[arg-type]
    # Best-effort: never raises.
    await svc.refresh_connection_metadata("gfs-1")
    got = await repo.get("gfs-1")
    assert got is not None
    # Name untouched on error.
    assert got.display_name == "GFS gfs-1"


async def test_refresh_connection_metadata_noop_for_unknown_gfs(env):
    _, repo = env
    session = _StubSession(status=200, body={"server_name": "Whatever"})
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    # No connection row → returns without touching the network.
    await svc.refresh_connection_metadata("nope")
    assert session.calls == []


async def test_refresh_connection_metadata_ignores_missing_server_name(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1", status="active", inbox_url="https://gfs.test"))
    session = _StubSession(status=200, body={})
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    await svc.refresh_connection_metadata("gfs-1")
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.display_name == "GFS gfs-1"


# ── Service: report_fraud ──────────────────────────────────────────────


async def test_report_fraud_signs_and_posts(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1", status="active", inbox_url="https://gfs.test"))
    session = _StubSession(status=200, body={"status": "recorded"})
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    ok = await svc.report_fraud(
        "gfs-1",
        target_type="space",
        target_id="s-1",
        category="spam",
        notes="bad",
        reporter_instance_id="me.home",
        reporter_user_id="u-1",
        signing_key=b"\x01" * 32,
    )
    assert ok is True
    assert session.calls and session.calls[0][0] == "POST"
    assert session.calls[0][1].endswith("/gfs/report")


async def test_report_fraud_returns_false_on_http_error(env):
    _, repo = env
    await repo.save(_make_conn("gfs-2", status="active", inbox_url="https://gfs.test"))
    session = _StubSession(
        status=500,
        body={},
    )
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    ok = await svc.report_fraud(
        "gfs-2",
        target_type="instance",
        target_id="peer.home",
        category="spam",
        notes=None,
        reporter_instance_id="me.home",
        reporter_user_id=None,
        signing_key=b"\x02" * 32,
    )
    assert ok is False


async def test_report_fraud_returns_false_for_unknown_gfs(env):
    _, repo = env
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    ok = await svc.report_fraud(
        "nope",
        target_type="space",
        target_id="s-1",
        category="spam",
        notes=None,
        reporter_instance_id="me.home",
        reporter_user_id=None,
        signing_key=b"\x03" * 32,
    )
    assert ok is False
    assert session.calls == []


async def test_disconnect_deletes_connection(env):
    _, repo = env
    await repo.save(_make_conn("rm-1"))
    svc = GfsConnectionService(repo, http_client=_StubSession())  # type: ignore[arg-type]
    await svc.disconnect("rm-1")
    assert await repo.get("rm-1") is None


async def test_disconnect_unknown_raises(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())  # type: ignore[arg-type]
    with pytest.raises(GfsConnectionError):
        await svc.disconnect("nope")


async def test_publish_space_records_local(env):
    session = _StubSession(status=200)
    svc = await _publishable_svc(env, session, "pub-1", space_id="space-x")
    pub = await svc.publish_space("space-x", "pub-1")
    # Post to publish endpoint happened.
    assert session.calls
    assert session.calls[0][0] == "POST"
    assert "/gfs/spaces/space-x/publish" in session.calls[0][1]
    # Returns the persisted publication; default status when the GFS
    # body carries none.
    assert pub.space_id == "space-x"
    assert pub.gfs_connection_id == "pub-1"
    assert pub.status == "active"


async def test_publish_space_returns_gfs_status(env):
    """The status the GFS returns in the publish body flows back into
    the returned publication AND the persisted row (e.g. ``pending``
    when the GFS requires moderator approval)."""
    session = _StubSession(status=200, body={"status": "pending"})
    svc = await _publishable_svc(env, session, "pub-2", space_id="space-p")
    _, repo = env
    pub = await svc.publish_space("space-p", "pub-2")
    assert pub.status == "pending"
    rows = await repo.list_publications_for_space("space-p")
    assert len(rows) == 1
    assert rows[0].status == "pending"


async def test_publish_space_raises_on_non_2xx_and_skips_local_row(env):
    """A rejecting GFS surfaces as ``GfsConnectionError`` and does NOT
    write a local publication row — a failed publish is no longer
    indistinguishable from success."""
    session = _StubSession(status=500)
    svc = await _publishable_svc(env, session, "pub-3", space_id="space-e")
    _, repo = env
    with pytest.raises(GfsConnectionError, match="HTTP 500"):
        await svc.publish_space("space-e", "pub-3")
    assert await repo.list_publications_for_space("space-e") == []


async def test_publish_space_raises_on_network_error_and_skips_local_row(env):
    class _RaisingSession:
        def post(self, *a, **kw):
            import aiohttp

            raise aiohttp.ClientError("unreachable")

    svc = await _publishable_svc(env, _RaisingSession(), "pub-4", space_id="space-n")
    _, repo = env
    with pytest.raises(GfsConnectionError, match="reach GFS"):
        await svc.publish_space("space-n", "pub-4")
    assert await repo.list_publications_for_space("space-n") == []


class _TimeoutSession:
    """Stub session whose request raises a *bare* ``asyncio.TimeoutError``.

    Mirrors what aiohttp's ``ClientTimeout(total=...)`` raises when the
    request hangs — NOT an ``aiohttp.ClientError`` subclass. The transport
    guard must still map it to ``GfsConnectionError``.
    """

    def post(self, *a, **kw):
        raise asyncio.TimeoutError

    def delete(self, *a, **kw):
        raise asyncio.TimeoutError


async def test_publish_space_maps_timeout_to_gfs_error(env):
    """A hung GFS raises a bare ``asyncio.TimeoutError`` from the total
    timeout; ``publish_space`` must map it to ``GfsConnectionError`` (→ 422)
    and leave no local row, not leak the raw timeout (→ 500)."""
    svc = await _publishable_svc(env, _TimeoutSession(), "pub-to", space_id="space-to")
    _, repo = env
    with pytest.raises(GfsConnectionError, match="reach GFS"):
        await svc.publish_space("space-to", "pub-to")
    assert await repo.list_publications_for_space("space-to") == []


async def test_unpublish_space_maps_timeout_to_gfs_error(env):
    """Symmetric with publish: a bare ``asyncio.TimeoutError`` on unpublish
    maps to ``GfsConnectionError`` and keeps the local row."""
    _, repo = env
    await repo.save(_make_conn("up-to"))
    await repo.publish_space("space-uto", "up-to")
    svc, _pk = _signing_svc(repo, _TimeoutSession())
    with pytest.raises(GfsConnectionError, match="reach GFS"):
        await svc.unpublish_space("space-uto", "up-to")
    rows = await repo.list_publications_for_space("space-uto")
    assert len(rows) == 1


async def test_publish_space_to_all_skips_timed_out_gfs(env):
    """One GFS that times out must NOT abort the whole fan-out — the bare
    ``asyncio.TimeoutError`` is caught as ``GfsConnectionError`` and skipped,
    so the healthy GFS is still published to."""

    class _MixedSession:
        """``post`` times out for the slow GFS, succeeds for the healthy one."""

        def post(self, url, **kw):
            if "slow.example" in url:
                raise asyncio.TimeoutError
            return _StubResp(200, {"status": "active"})

    svc = await _publishable_svc(env, _MixedSession(), "ok-gfs", space_id="space-fan")
    _, repo = env
    # Re-home the first conn's URL + add a second (slow) GFS.
    await repo.save(_make_conn("ok-gfs", inbox_url="https://ok.example"))
    await repo.save(_make_conn("slow-gfs", inbox_url="https://slow.example"))
    published = await svc.publish_space_to_all("space-fan")
    assert published == 1
    # Only the healthy GFS got a local publication row.
    rows = await repo.list_publications_for_space("space-fan")
    assert len(rows) == 1
    assert rows[0].gfs_connection_id == "ok-gfs"


async def test_publish_space_handles_non_dict_json_body(env):
    """A GFS that returns valid-but-non-object JSON (e.g. ``[]``) on a 200
    must not crash the status parse — coerce to ``{}`` and default to
    ``active``."""

    class _NonDictResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return []  # valid JSON, but not an object

        async def text(self):
            return ""

    class _NonDictSession:
        def post(self, *a, **kw):
            return _NonDictResp()

    svc = await _publishable_svc(env, _NonDictSession(), "pub-nd", space_id="space-nd")
    _, repo = env
    pub = await svc.publish_space("space-nd", "pub-nd")
    assert pub.status == "active"
    rows = await repo.list_publications_for_space("space-nd")
    assert len(rows) == 1
    assert rows[0].status == "active"


async def test_unpublish_space_records_local(env):
    _, repo = env
    await repo.save(_make_conn("up-1"))
    session = _StubSession(status=200)
    svc, _pk = _signing_svc(repo, session)
    await svc.unpublish_space("space-y", "up-1")
    # POST, not DELETE: the GFS accepts both, and some proxies strip a
    # DELETE body — which would turn the now-signed unpublish into a
    # permanent 400.
    assert session.calls and session.calls[0][0] == "POST"


async def test_unpublish_space_404_treated_as_success(env):
    """The space is already absent on the GFS — idempotent delete, so
    a 404 is success and the local row is removed without raising."""
    _, repo = env
    await repo.save(_make_conn("up-404"))
    await repo.publish_space("space-z", "up-404")
    session = _StubSession(status=404)
    svc, _pk = _signing_svc(repo, session)
    await svc.unpublish_space("space-z", "up-404")
    assert await repo.list_publications_for_space("space-z") == []


async def test_unpublish_space_raises_on_500_and_keeps_local_row(env):
    _, repo = env
    await repo.save(_make_conn("up-500"))
    await repo.publish_space("space-k", "up-500")
    session = _StubSession(status=500)
    svc, _pk = _signing_svc(repo, session)
    with pytest.raises(GfsConnectionError, match="HTTP 500"):
        await svc.unpublish_space("space-k", "up-500")
    rows = await repo.list_publications_for_space("space-k")
    assert len(rows) == 1


async def test_update_status(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.update_status("gfs-1", "suspended")
    got = await repo.get("gfs-1")
    assert got is not None
    assert got.status == "suspended"


async def test_delete_removes_connection_and_publications(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    await repo.delete("gfs-1")
    assert await repo.get("gfs-1") is None
    pubs = await repo.list_publications("gfs-1")
    assert pubs == []


async def test_publish_and_unpublish_space(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert len(pubs) == 1
    assert pubs[0].space_id == "sp-1"

    await repo.unpublish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert pubs == []


async def test_publish_space_idempotent(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    await repo.publish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert len(pubs) == 1


async def test_list_gfs_for_space(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.save(_make_conn("gfs-2"))
    await repo.publish_space("sp-1", "gfs-1")
    await repo.publish_space("sp-1", "gfs-2")
    conns = await repo.list_gfs_for_space("sp-1")
    assert {c.id for c in conns} == {"gfs-1", "gfs-2"}


async def test_count_published_spaces(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    assert await repo.count_published_spaces("gfs-1") == 0
    await repo.publish_space("sp-1", "gfs-1")
    await repo.publish_space("sp-2", "gfs-1")
    assert await repo.count_published_spaces("gfs-1") == 2


# ─── Service tests ──────────────────────────────────────────────────────


_OWN_PAIR_KW = {
    "own_instance_id": "alpha.home",
    "own_public_key_hex": "aa" * 32,
    "own_inbox_url": "https://alpha.example/federation/inbox",
    "own_display_name": "Alpha House",
}


async def test_pair_success(env):
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "remote-gfs-id",
                    "public_key": "bb" * 32,
                    "server_name": "Test GFS",
                    "base_url": "https://gfs.example.com",
                },
            ),
            "POST": (200, {"status": "registered", "instance_id": "alpha.home"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session)
    conn = await svc.pair(
        {"gfs_url": "https://gfs.example.com", "token": "tok-123"},
        **_OWN_PAIR_KW,
    )
    assert conn.status == "active"
    assert conn.gfs_instance_id == "remote-gfs-id"
    # The display name comes from the GFS's own ``server_name`` (its
    # branding), not anything the SH made up locally.
    assert conn.display_name == "Test GFS"
    # The pinned public key is the GFS's, fetched via ``/gfs/info`` —
    # this is the trust anchor for every subsequent relay.
    assert conn.public_key == "bb" * 32
    # Two calls: GET /gfs/info, then POST /gfs/register.
    assert session.calls == [
        ("GET", "https://gfs.example.com/gfs/info"),
        ("POST", "https://gfs.example.com/gfs/register"),
    ]
    # Saved to repo.
    saved = await repo.get(conn.id)
    assert saved is not None


async def test_pair_ships_keywrap_pubkey_and_kem_suite(env):
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "remote-gfs-id",
                    "public_key": "bb" * 32,
                    "server_name": "Test GFS",
                },
            ),
            "POST": (200, {"status": "registered"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session)
    await svc.pair(
        {"gfs_url": "https://gfs.example.com", "token": "tok"},
        **_OWN_PAIR_KW,
        own_keywrap_public_key_hex="ee" * 32,
        own_keywrap_sig="c2ln",
    )
    body = session._last_body
    assert body is not None
    assert body["keywrap_public_key"] == "ee" * 32
    assert body["kem_suite"] == "x25519"
    assert body["keywrap_sig"] == "c2ln"


async def test_pair_omits_keywrap_when_unavailable(env):
    """A caller that passes no key-wrap pubkey → empty field, no kem_suite
    claim (an older/unprovisioned HFS can't seal yet — graceful)."""
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "remote",
                    "public_key": "bb" * 32,
                    "server_name": "GFS",
                },
            ),
            "POST": (200, {"status": "registered"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session)
    await svc.pair(
        {"gfs_url": "https://gfs", "token": "tok"},
        **_OWN_PAIR_KW,
    )
    body = session._last_body
    assert body is not None
    assert body.get("keywrap_public_key", "") == ""
    assert body.get("kem_suite", "") == ""


async def test_pair_pending_status(env):
    """A GFS with auto-accept disabled returns ``status="pending"``;
    the local connection lands as ``pending`` (not ``active``) so the
    UI can render the "awaiting GFS admin review" state."""
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "remote",
                    "public_key": "cc" * 32,
                    "server_name": "GFS",
                    "base_url": "https://gfs",
                },
            ),
            "POST": (200, {"status": "pending"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session)
    conn = await svc.pair(
        {"gfs_url": "https://gfs", "token": "tok"},
        **_OWN_PAIR_KW,
    )
    assert conn.status == "pending"


async def test_pair_missing_qr_fields(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="QR payload"):
        await svc.pair({"gfs_url": "https://x.com"}, **_OWN_PAIR_KW)


async def test_pair_missing_own_identity(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="own_instance_id"):
        await svc.pair(
            {"gfs_url": "https://x.com", "token": "tok"},
            own_instance_id="",
            own_public_key_hex="ab",
            own_inbox_url="https://x",
        )


async def test_pair_gfs_info_unreachable(env):
    """A GFS that doesn't expose ``/gfs/info`` cannot be pinned —
    surface the failure cleanly instead of saving a half-trusted
    connection."""
    _, repo = env
    session = _StubSession(
        method_responses={"GET": (404, {})},
    )
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsConnectionError, match="HTTP 404"):
        await svc.pair(
            {"gfs_url": "https://gfs.example.com", "token": "tok"},
            **_OWN_PAIR_KW,
        )


async def test_pair_register_rejects(env):
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "remote",
                    "public_key": "cc" * 32,
                    "server_name": "GFS",
                    "base_url": "https://gfs",
                },
            ),
            "POST": (401, {}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsConnectionError, match="HTTP 401"):
        await svc.pair(
            {"gfs_url": "https://gfs.example.com", "token": "stale-tok"},
            **_OWN_PAIR_KW,
        )


async def test_pair_no_public_key_in_info(env):
    """``/gfs/info`` must return both ``gfs_instance_id`` and
    ``public_key`` — without the key there's no anchor to verify
    later reports against, so refuse to register."""
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (200, {"gfs_instance_id": "remote", "public_key": ""}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsConnectionError, match="gfs_instance_id and public_key"):
        await svc.pair(
            {"gfs_url": "https://gfs.example.com", "token": "tok"},
            **_OWN_PAIR_KW,
        )


async def test_disconnect_success(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    svc = GfsConnectionService(repo, http_client=_StubSession())
    await svc.disconnect("gfs-1")
    assert await repo.get("gfs-1") is None


async def test_disconnect_not_found(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="not found"):
        await svc.disconnect("nonexistent")


async def test_list_connections(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1", status="active"))
    await repo.save(_make_conn("gfs-2", status="suspended"))
    await repo.save(_make_conn("gfs-3", status="pending"))
    svc = GfsConnectionService(repo, http_client=_StubSession())
    result = await svc.list_connections()
    # The UI list surfaces every status — a pending/suspended connection
    # must not be invisible just because the GFS hasn't approved yet.
    assert {c.id for c in result} == {"gfs-1", "gfs-2", "gfs-3"}
    assert {c.status for c in result} == {"active", "suspended", "pending"}


async def test_publish_space_success(env):
    session = _StubSession(status=200)
    svc = await _publishable_svc(env, session, "gfs-1", space_id="sp-1")
    _, repo = env
    pub = await svc.publish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert len(pubs) == 1
    assert pubs[0].space_id == "sp-1"
    assert pub.space_id == "sp-1"
    assert len(session.calls) == 1


async def test_publish_space_not_found(env):
    _, repo = env
    svc = GfsConnectionService(repo, http_client=_StubSession())
    with pytest.raises(GfsConnectionError, match="not found"):
        await svc.publish_space("sp-1", "nonexistent")


async def test_unpublish_space_success(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    await repo.publish_space("sp-1", "gfs-1")
    session = _StubSession(status=204)
    svc, _pk = _signing_svc(repo, session)
    await svc.unpublish_space("sp-1", "gfs-1")
    pubs = await repo.list_publications("gfs-1")
    assert pubs == []


async def test_unpublish_space_not_found(env):
    _, repo = env
    svc, _pk = _signing_svc(repo, _StubSession())
    with pytest.raises(GfsConnectionError, match="not found"):
        await svc.unpublish_space("sp-1", "nonexistent")


# ─── Sync-signaling round-robin (spec §24.10.7) ───────────────────────


async def test_request_signaling_node_returns_url(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    kp = generate_identity_keypair()
    session = _StubSession(
        status=200,
        body={"signaling_node": "https://b.gfs.test", "session_id": "s1"},
    )
    svc = GfsConnectionService(repo, http_client=session)
    result = await svc.request_signaling_node(
        "s1",
        from_instance="caller.home",
        signing_key=kp.private_key,
    )
    assert result == "https://b.gfs.test"
    # Posted to the right URL with a signature attached.
    assert session.calls == [
        ("POST", "https://gfs.example.com/cluster/signaling-session")
    ]


async def test_request_signaling_node_503_returns_none(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    kp = generate_identity_keypair()
    session = _StubSession(status=503, body={"reason": "node_capacity"})
    svc = GfsConnectionService(repo, http_client=session)
    result = await svc.request_signaling_node(
        "s1",
        from_instance="caller.home",
        signing_key=kp.private_key,
    )
    assert result is None


async def test_request_signaling_node_null_returns_none(env):
    """Single-node GFS: ``signaling_node: null`` → None."""
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    kp = generate_identity_keypair()
    session = _StubSession(status=200, body={"signaling_node": None})
    svc = GfsConnectionService(repo, http_client=session)
    result = await svc.request_signaling_node(
        "s1",
        from_instance="caller.home",
        signing_key=kp.private_key,
    )
    assert result is None


async def test_request_signaling_node_no_active_gfs_returns_none(env):
    """No paired active GFS → None (HFS-only deployment)."""
    _, repo = env
    kp = generate_identity_keypair()
    svc = GfsConnectionService(repo, http_client=_StubSession())
    result = await svc.request_signaling_node(
        "s1",
        from_instance="caller.home",
        signing_key=kp.private_key,
    )
    assert result is None


async def test_release_signaling_node_posts(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    kp = generate_identity_keypair()
    session = _StubSession(status=200, body={"status": "released"})
    svc = GfsConnectionService(repo, http_client=session)
    await svc.release_signaling_node(
        "s1",
        "https://b.gfs.test",
        from_instance="caller.home",
        signing_key=kp.private_key,
    )
    assert session.calls == [
        ("POST", "https://gfs.example.com/cluster/signaling-session/release"),
    ]


async def test_release_signaling_node_no_url_is_noop(env):
    _, repo = env
    await repo.save(_make_conn("gfs-1"))
    kp = generate_identity_keypair()
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session)
    await svc.release_signaling_node(
        "s1",
        "",
        from_instance="caller.home",
        signing_key=kp.private_key,
    )
    assert session.calls == []


# ── publish-space context (attach_publish_context + _build_publish_body) ──


async def test_publish_body_raises_when_context_unset(env):
    """Without ``attach_publish_context`` there's no signing key, so the
    GFS publish is fail-closed: ``publish_space`` raises rather than send
    an unsigned body (the GFS now rejects unsigned publishes)."""
    _, repo = env
    await repo.save(_make_conn("gfs-1", inbox_url="https://gfs.example"))
    session = _StubSession(method_responses={"POST": (200, {"status": "pending"})})
    svc = GfsConnectionService(repo, http_client=session)
    # No attach_publish_context call → no signing key → must refuse.
    with pytest.raises(GfsConnectionError):
        await svc.publish_space("sp-bare", "gfs-1")
    # And no HTTP request was made.
    assert session.calls == []


async def test_publish_body_carries_metadata_and_signature(env):
    """With ``attach_publish_context`` wired, the publish body includes
    the local space's name + description + signed canonical JSON the
    GFS verifies against ``ClientInstance.public_key``."""
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-2", inbox_url="https://gfs.example"))
    space_repo = SqliteSpaceRepo(db)
    space = Space(
        id="sp-rich",
        name="Local Birds",
        owner_instance_id="alpha.home",
        owner_username="alice",
        identity_public_key="aa" * 32,
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
        description="everyday birds in the neighbourhood",
    )
    await space_repo.save(space)

    kp = generate_identity_keypair()
    session = _StubSession(
        method_responses={"POST": (200, {"status": "registered"})},
    )
    svc = GfsConnectionService(conn_repo, http_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    await svc.publish_space("sp-rich", "gfs-2")
    # Capture the body the stub session forwarded.
    body = session._last_body  # type: ignore[attr-defined]
    assert body["space_id"] == "sp-rich"
    assert body["owning_instance"] == "alpha.home"
    assert body["name"] == "Local Birds"
    assert body["description"] == "everyday birds in the neighbourhood"
    # Phase 5a: the publish body ships the space's Ed25519 authority verify key
    # so the GFS can TOFU-pin it for space-authority-signed relays.
    assert body["identity_public_key"] == "aa" * 32
    sig_b64 = body.pop("signature")
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig_b64))


async def test_publish_body_carries_brand_colors_and_image_data_uris(env):
    """With theme/cover/icon repos wired, the publish body ships the real
    theme colours + the cover and icon as self-contained data URIs (so the
    GFS public page renders the space's brand on its own origin)."""
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )
    from socialhome.repositories.space_cover_repo import SqliteSpaceCoverRepo
    from socialhome.repositories.space_icon_repo import SqliteSpaceIconRepo
    from socialhome.repositories.space_repo import SqliteSpaceRepo
    from socialhome.repositories.theme_repo import SqliteThemeRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-b", inbox_url="https://gfs.example"))
    space_repo = SqliteSpaceRepo(db)
    space = Space(
        id="sp-brand",
        name="Brandy",
        owner_instance_id="alpha.home",
        owner_username="alice",
        identity_public_key="aa" * 32,
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
    )
    await space_repo.save(space)
    await space_repo.set_cover_hash("sp-brand", "ch")
    await space_repo.set_icon_hash("sp-brand", "ih")
    theme_repo = SqliteThemeRepo(db)
    await theme_repo.upsert_space(
        space_id="sp-brand", primary_color="#112233", accent_color="#445566"
    )
    cover_repo = SqliteSpaceCoverRepo(db)
    await cover_repo.set(
        "sp-brand", bytes_webp=b"RIFFcover", hash="ch", width=8, height=8
    )
    icon_repo = SqliteSpaceIconRepo(db)
    await icon_repo.set(
        "sp-brand", bytes_webp=b"RIFFicon", hash="ih", width=8, height=8
    )

    kp = generate_identity_keypair()
    session = _StubSession(method_responses={"POST": (200, {"status": "ok"})})
    svc = GfsConnectionService(conn_repo, http_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
        theme_repo=theme_repo,
        cover_repo=cover_repo,
        icon_repo=icon_repo,
    )
    await svc.publish_space("sp-brand", "gfs-b")
    body = session._last_body  # type: ignore[attr-defined]
    assert body["primary_color"] == "#112233"
    assert body["accent_color"] == "#445566"
    assert body["cover_url"].startswith("data:image/webp;base64,")
    assert body["icon_url"].startswith("data:image/webp;base64,")


# ── update_display_name_to_all ──────────────────────────────────────────


async def test_update_display_name_signs_and_posts_to_each_gfs(env):
    """The household rename is signed over the canonical
    ``{instance_id, display_name, ts}`` JSON and POSTed to every active
    GFS's ``/gfs/instance``; the return value is the number of 200s and
    the signature verifies byte-for-byte against the GFS contract."""
    _, repo = env
    await repo.save(_make_conn("g1", inbox_url="https://a.example"))
    await repo.save(_make_conn("g2", inbox_url="https://b.example"))
    kp = generate_identity_keypair()
    session = _StubSession(status=200, body={"status": "ok"})
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    n = await svc.update_display_name_to_all("Casa Vizeli")
    assert n == 2
    # POSTed to each GFS's /gfs/instance.
    assert ("POST", "https://a.example/gfs/instance") in session.calls
    assert ("POST", "https://b.example/gfs/instance") in session.calls
    body = session._last_body  # type: ignore[attr-defined]
    assert body["instance_id"] == "alpha.home"
    assert body["display_name"] == "Casa Vizeli"
    assert body["ts"]
    sig = body["signature"]
    assert sig
    canonical = json.dumps(
        {
            "instance_id": body["instance_id"],
            "display_name": body["display_name"],
            "ts": body["ts"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig))


async def test_update_display_name_skips_404_old_gfs(env):
    """A GFS too old to have ``/gfs/instance`` returns 404 — skipped,
    counted as 0, never raising."""
    _, repo = env
    await repo.save(_make_conn("g1", inbox_url="https://old.example"))
    kp = generate_identity_keypair()
    session = _StubSession(status=404)
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    n = await svc.update_display_name_to_all("New Name")
    assert n == 0


async def test_update_display_name_skips_transport_errors(env):
    """A GFS raising ClientError or a bare TimeoutError is skipped; the
    healthy GFS is still updated, and the method never raises."""
    _, repo = env
    await repo.save(_make_conn("ok", inbox_url="https://ok.example"))
    await repo.save(_make_conn("err", inbox_url="https://err.example"))
    await repo.save(_make_conn("slow", inbox_url="https://slow.example"))
    kp = generate_identity_keypair()

    class _MixedSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def post(self, url, **kw):
            self.calls.append(("POST", url))
            if "err.example" in url:
                raise aiohttp.ClientError("boom")
            if "slow.example" in url:
                raise asyncio.TimeoutError
            return _StubResp(200, {"status": "ok"})

    session = _MixedSession()
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    n = await svc.update_display_name_to_all("New Name")
    assert n == 1
    assert len(session.calls) == 3


async def test_update_display_name_no_context_returns_zero(env):
    """Without publish context wired (no instance id / signing key),
    the method is a no-op returning 0 — never crashes early boot."""
    _, repo = env
    await repo.save(_make_conn("g1", inbox_url="https://a.example"))
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    n = await svc.update_display_name_to_all("New Name")
    assert n == 0
    assert session.calls == []


# ── push_display_name (single GFS, reconnect self-heal) ─────────────────


async def test_push_display_name_signs_and_posts_to_one_gfs(env):
    """``push_display_name`` POSTs the signed canonical
    ``{instance_id, display_name, ts}`` body to exactly the named GFS's
    ``/gfs/instance`` and returns True on 200. The signature verifies
    byte-for-byte against the GFS contract — same shape as the
    fan-out variant, but to one connection."""
    _, repo = env
    await repo.save(_make_conn("g1", inbox_url="https://a.example"))
    await repo.save(_make_conn("g2", inbox_url="https://b.example"))
    kp = generate_identity_keypair()
    session = _StubSession(status=200, body={"status": "ok"})
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    ok = await svc.push_display_name("g1", "Casa Vizeli")
    assert ok is True
    # POSTed only to g1's /gfs/instance, not g2's.
    assert ("POST", "https://a.example/gfs/instance") in session.calls
    assert ("POST", "https://b.example/gfs/instance") not in session.calls
    body = session._last_body  # type: ignore[attr-defined]
    assert body["instance_id"] == "alpha.home"
    assert body["display_name"] == "Casa Vizeli"
    assert body["ts"]
    sig = body["signature"]
    assert sig
    canonical = json.dumps(
        {
            "instance_id": body["instance_id"],
            "display_name": body["display_name"],
            "ts": body["ts"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig))


async def test_push_display_name_returns_false_on_http_error(env):
    """A non-200 (e.g. 404 from an old GFS, or a 5xx) yields False and
    never raises."""
    _, repo = env
    await repo.save(_make_conn("g1", inbox_url="https://old.example"))
    kp = generate_identity_keypair()
    session = _StubSession(status=404)
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    ok = await svc.push_display_name("g1", "New Name")
    assert ok is False


async def test_push_display_name_returns_false_on_transport_error(env):
    """A GFS raising ClientError/TimeoutError is swallowed — best-effort
    push returns False, never propagates."""
    _, repo = env
    await repo.save(_make_conn("g1", inbox_url="https://err.example"))
    kp = generate_identity_keypair()

    class _ErrSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def post(self, url, **kw):
            self.calls.append(("POST", url))
            raise aiohttp.ClientError("boom")

    session = _ErrSession()
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    ok = await svc.push_display_name("g1", "New Name")
    assert ok is False
    assert len(session.calls) == 1


async def test_push_display_name_returns_false_for_unknown_gfs(env):
    """A gfs_id with no active connection row → False, no POST."""
    _, repo = env
    kp = generate_identity_keypair()
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    ok = await svc.push_display_name("nope", "New Name")
    assert ok is False
    assert session.calls == []


async def test_push_display_name_no_context_returns_false(env):
    """Without publish context wired (no instance id / signing key),
    the push is a no-op returning False — never crashes early boot."""
    _, repo = env
    await repo.save(_make_conn("g1", inbox_url="https://a.example"))
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session)  # type: ignore[arg-type]
    ok = await svc.push_display_name("g1", "New Name")
    assert ok is False
    assert session.calls == []


async def test_publish_body_raises_when_space_missing(env):
    """``attach_publish_context`` is wired but the local space row is
    gone — a publish with no space metadata can't be signed into a valid
    body, so fail closed (raise) rather than send a partial / unsigned
    publish the GFS will reject anyway."""
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-3", inbox_url="https://gfs.example"))
    kp = generate_identity_keypair()
    session = _StubSession(method_responses={"POST": (200, {"status": "pending"})})
    svc = GfsConnectionService(conn_repo, http_client=session)
    svc.attach_publish_context(
        space_repo=SqliteSpaceRepo(db),
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    with pytest.raises(GfsConnectionError):
        await svc.publish_space("sp-missing", "gfs-3")
    assert session.calls == []


async def test_subscribe_to_gfs_space_signs_body(env):
    """``subscribe_to_gfs_space`` signs the canonical
    ``{action, instance_id, space_id, ts}`` body with the household
    identity key and POSTs it to the GFS ``/gfs/subscribe`` endpoint.
    The ``action`` rides inside the signed bytes (domain separation), so
    the GFS can't have this signature replayed as an unsubscribe."""
    _, repo = env
    await repo.save(_make_conn("gfs-sub", inbox_url="https://gfs.example"))
    kp = generate_identity_keypair()
    session = _StubSession(method_responses={"POST": (200, {"status": "subscribed"})})
    svc = GfsConnectionService(repo, http_client=session)
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    await svc.subscribe_to_gfs_space("sp-join", "gfs-sub")
    assert session.calls == [
        ("POST", "https://gfs.example/gfs/subscribe"),
    ]
    body = session._last_body  # type: ignore[attr-defined]
    assert body["action"] == "subscribe"
    assert body["instance_id"] == "alpha.home"
    assert body["space_id"] == "sp-join"
    assert body["ts"]
    sig = body["signature"]
    canonical = json.dumps(
        {
            "action": "subscribe",
            "instance_id": "alpha.home",
            "space_id": "sp-join",
            "ts": body["ts"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig))


async def test_unpublish_space_signs_canonical_body(env):
    """SECURITY: ``unpublish`` is owner-authenticated on the GFS, so the HFS
    must ship a signed ``{owning_instance, ts, signature}`` body — the
    signature covering the canonical ``{action: "unpublish", owning_instance,
    space_id, ts}`` bytes (``action`` inside, so a captured subscribe
    signature can't be replayed as a delisting)."""
    _, repo = env
    await repo.save(_make_conn("gfs-un", inbox_url="https://gfs.example"))
    await repo.publish_space("sp-un", "gfs-un")
    session = _StubSession(status=200)
    svc, pubkey = _signing_svc(repo, session)
    await svc.unpublish_space("sp-un", "gfs-un")

    assert session.calls == [
        ("POST", "https://gfs.example/gfs/spaces/sp-un/unpublish"),
    ]
    body = session._last_body  # type: ignore[attr-defined]
    assert body is not None
    assert body["owning_instance"] == "alpha.home"
    assert body["ts"]
    canonical = json.dumps(
        {
            "action": "unpublish",
            "owning_instance": "alpha.home",
            "space_id": "sp-un",
            "ts": body["ts"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert verify_ed25519(pubkey, canonical, b64url_decode(body["signature"]))
    # Local publication row is cleared on the successful round-trip.
    assert await repo.list_publications_for_space("sp-un") == []


async def test_unpublish_space_raises_without_signing_key(env):
    """No identity wired → fail closed rather than send a body the GFS
    rejects (mirrors ``subscribe_to_gfs_space``); the local row survives."""
    _, repo = env
    await repo.save(_make_conn("gfs-un2", inbox_url="https://gfs.example"))
    await repo.publish_space("sp-un2", "gfs-un2")
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsConnectionError):
        await svc.unpublish_space("sp-un2", "gfs-un2")
    assert session.calls == []
    assert len(await repo.list_publications_for_space("sp-un2")) == 1


async def test_subscribe_to_gfs_space_raises_without_signing_key(env):
    """No identity wired → fail closed; the GFS rejects unsigned subscribes."""
    _, repo = env
    await repo.save(_make_conn("gfs-sub2", inbox_url="https://gfs.example"))
    session = _StubSession(method_responses={"POST": (200, {"status": "subscribed"})})
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsConnectionError):
        await svc.subscribe_to_gfs_space("sp-join", "gfs-sub2")
    assert session.calls == []


# ─── publish_space_event (Phase 5a2 — relay a space event to the GFS) ──────


class _RecordingSession:
    """aiohttp-session stub that records EVERY POST body + url.

    The shared :class:`_StubSession` keeps only the last body; the
    fan-out tests need every (url, body) pair to assert the relay hit
    each published GFS.
    """

    def __init__(self, status: int = 200) -> None:
        self._status = status
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _StubResp(self._status, {"status": "published"})


async def _publish_event_svc(env, session, *, space_id: str, gfs_ids: list[str]):
    """Service wired for publish_space_event, with the space published to
    each of *gfs_ids* (so ``list_gfs_for_space`` returns them)."""
    db, conn_repo = env
    for gid in gfs_ids:
        await conn_repo.save(_make_conn(gid, inbox_url=f"https://{gid}.example"))
        await conn_repo.publish_space(space_id, gid)
    kp = generate_identity_keypair()
    svc = GfsConnectionService(conn_repo, http_client=session)
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    return svc, kp


async def test_publish_space_event_signs_and_fans_to_each_published_gfs(env):
    """A relay event is POSTed to ``/gfs/publish`` on EVERY GFS the space
    is published to, carrying the verbatim envelope as ``payload`` and a
    valid household transport signature over the canonical body."""
    session = _RecordingSession()
    svc, kp = await _publish_event_svc(
        env, session, space_id="sp-relay", gfs_ids=["g1", "g2"]
    )
    envelope = {"space_id": "sp-relay", "epoch": 0, "encrypted_payload": "ct"}
    delivered = await svc.publish_space_event(
        space_id="sp-relay",
        event_type="space_post_public",
        payload=envelope,
        from_instance="alpha.home",
    )
    assert delivered == 2
    urls = sorted(u for u, _ in session.posts)
    assert urls == ["https://g1.example/gfs/publish", "https://g2.example/gfs/publish"]
    # Body shape + signature verifies over the canonical {space_id,
    # event_type, payload, from_instance} (signature stripped).
    _, body = session.posts[0]
    assert body["space_id"] == "sp-relay"
    assert body["event_type"] == "space_post_public"
    assert body["payload"] == envelope
    assert body["from_instance"] == "alpha.home"
    sig = body.pop("signature")
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig))


async def test_publish_space_event_returns_zero_without_signing_key(env):
    """No identity wired → fail closed, no POST."""
    _, repo = env
    await repo.save(_make_conn("g-x", inbox_url="https://gx.example"))
    await repo.publish_space("sp-x", "g-x")
    session = _RecordingSession()
    svc = GfsConnectionService(repo, http_client=session)
    delivered = await svc.publish_space_event(
        space_id="sp-x",
        event_type="space_post_public",
        payload={"space_id": "sp-x"},
        from_instance="alpha.home",
    )
    assert delivered == 0
    assert session.posts == []


async def test_publish_space_event_skips_unpublished_space(env):
    """A space published to no GFS → nothing sent."""
    session = _RecordingSession()
    svc, _ = await _publish_event_svc(env, session, space_id="sp-pub", gfs_ids=[])
    delivered = await svc.publish_space_event(
        space_id="sp-none",
        event_type="space_post_public",
        payload={"space_id": "sp-none"},
        from_instance="alpha.home",
    )
    assert delivered == 0
    assert session.posts == []
