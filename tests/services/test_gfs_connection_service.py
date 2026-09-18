"""Tests for GfsConnectionService + SqliteGfsConnectionRepo."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    sign_authority_event,
)
from socialhome.crypto import (
    b64url_decode,
    derive_instance_id,
    generate_identity_keypair,
    verify_ed25519,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.global_server import create_gfs_app
from socialhome.global_server.app_keys import gfs_fed_repo_key
from socialhome.capabilities_sig import (
    CAPS_SIG_SUITE_ED25519,
    sign_capabilities,
)
from socialhome.global_server.domain import ClientInstance
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.services import gfs_connection_service
from socialhome.services.gfs_connection_service import (
    GFS_INFO_NEGATIVE_TTL_S,
    MAX_REMOTE_DETAIL_CHARS,
    GfsConnectionError,
    GfsConnectionService,
    _remote_detail,
)


# ─── Helpers ────────────────────────────────────────────────────────────


class _Content:
    """Minimal stand-in for ``aiohttp``'s streaming body reader."""

    __slots__ = ("_raw",)

    def __init__(self, raw: bytes):
        self._raw = raw

    async def read(self, n: int = -1) -> bytes:
        return self._raw if n < 0 else self._raw[:n]


class _StubResp:
    __slots__ = ("status", "_body", "_text", "content", "content_length")

    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status = status
        self._body = body or {}
        self._text = text
        self.content = _Content(text.encode())
        self.content_length = len(text.encode())

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


#: The GFS identity keypair the anonymous-publish tests pretend a household
#: pinned at pair time. The capability block on ``/gfs/info`` is signed with
#: it, and the household trusts the capability only through that signature.
_GFS_KP = generate_identity_keypair()


def _make_conn(
    gfs_id: str = "gfs-1",
    *,
    status: str = "active",
    inbox_url: str = "https://gfs.example.com",
    public_key: str = "pubkey-hex",
) -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"inst-{gfs_id}",
        display_name=f"GFS {gfs_id}",
        public_key=public_key,
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


@pytest.mark.parametrize(
    "gfs_url",
    [
        "http://gfs.example.com",
        "HTTP://Gfs.Example.Com",
        "http://8.8.8.8:9000",
        "http://gfs.example.com:8080/base",
        "ftp://gfs.example.com",
        "gfs.example.com",
    ],
)
async def test_pair_rejects_a_public_gfs_url_without_tls(env, gfs_url):
    """A GFS reachable over plain ``http://`` on the public internet is an
    on-path attacker's dream: ``/gfs/info`` is unauthenticated at the
    transport layer, so stripping the signed capability block (or swapping the
    pinned key on the very first TOFU fetch) is trivial. Refuse at pair time —
    before a single byte is sent — rather than pinning a downgradeable peer."""
    _, repo = env
    session = _StubSession(method_responses={"GET": (200, {})})
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsConnectionError, match="https"):
        await svc.pair({"gfs_url": gfs_url, "token": "tok"}, **_OWN_PAIR_KW)
    # Nothing left the household — the URL never reached the network.
    assert session.calls == []
    assert await repo.list_all() == []


@pytest.mark.parametrize(
    "gfs_url",
    [
        "http://127.0.0.1:8081",
        "http://localhost:9000",
        "http://[::1]:9000",
        "http://192.168.1.5",
        "http://10.0.0.7:8080",
        "http://172.16.4.2",
        "http://[fe80::1]:9000",
        "http://[fd00::5]",
    ],
)
async def test_pair_allows_plain_http_on_loopback_or_a_private_network(env, gfs_url):
    """A GFS on the LAN (or on the developer's loopback, which is how the
    federation demo harness pairs) has no public path to attack and often no
    certificate to serve, so plain ``http://`` stays allowed there."""
    _, repo = env
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {"gfs_instance_id": "lan-gfs", "public_key": "bb" * 32},
            ),
            "POST": (200, {"status": "registered"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session)
    conn = await svc.pair({"gfs_url": gfs_url, "token": "tok"}, **_OWN_PAIR_KW)
    assert conn.gfs_instance_id == "lan-gfs"


async def test_pair_rejects_a_public_plain_http_own_inbox_url(env):
    """The household's own federation base travels to the GFS as the address
    peers will POST to — a public plain-http inbox is the same downgrade
    surface from the other side."""
    _, repo = env
    session = _StubSession(method_responses={"GET": (200, {})})
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsConnectionError, match="https"):
        await svc.pair(
            {"gfs_url": "https://gfs.example.com", "token": "tok"},
            **{**_OWN_PAIR_KW, "own_inbox_url": "http://alpha.example/federation"},
        )
    assert session.calls == []


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
    # The membership gate travels so the directory can label the listing…
    assert body["join_mode"] == "open"
    # …and the readability opt-in travels separately, because it — not the
    # join mode — is what the GFS enforces on /gfs/subscribe. Off by default.
    assert body["allow_subscribers"] is False
    sig_b64 = body.pop("signature")
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig_b64))


async def test_publish_body_carries_allow_subscribers(env):
    """The readability opt-in travels INSIDE the signed canonical body — the
    GFS refuses a subscribe without it. Deliberately paired with
    ``invite_only`` here: the two dials are independent, and this broadcast
    shape (invited people post, anyone may follow) is exactly what the old
    join-mode-as-readability model could not express."""
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-rd", inbox_url="https://gfs.example"))
    space_repo = SqliteSpaceRepo(db)
    await space_repo.save(
        Space(
            id="sp-rd",
            name="Broadcast",
            owner_instance_id="alpha.home",
            owner_username="alice",
            identity_public_key="aa" * 32,
            config_sequence=0,
            features=SpaceFeatures(allow_subscribers=True),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    kp = generate_identity_keypair()
    session = _StubSession(method_responses={"POST": (200, {"status": "active"})})
    svc = GfsConnectionService(conn_repo, http_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    await svc.publish_space("sp-rd", "gfs-rd")
    body = session._last_body  # type: ignore[attr-defined]
    assert body["allow_subscribers"] is True
    assert body["join_mode"] == "invite_only"
    sig_b64 = body.pop("signature")
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig_b64))


async def test_publish_body_carries_invite_only_join_mode(env):
    """A global space whose owner keeps it invite-only says so on the wire:
    the GFS lists it for discovery but must refuse to seat a subscriber, so
    the value has to travel (it used to be absent entirely)."""
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-jm", inbox_url="https://gfs.example"))
    space_repo = SqliteSpaceRepo(db)
    await space_repo.save(
        Space(
            id="sp-jm",
            name="Quiet",
            owner_instance_id="alpha.home",
            owner_username="alice",
            identity_public_key="aa" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.GLOBAL,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    kp = generate_identity_keypair()
    session = _StubSession(method_responses={"POST": (200, {"status": "active"})})
    svc = GfsConnectionService(conn_repo, http_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    await svc.publish_space("sp-jm", "gfs-jm")
    body = session._last_body  # type: ignore[attr-defined]
    assert body["join_mode"] == "invite_only"
    sig_b64 = body.pop("signature")
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(sig_b64))


async def test_publish_body_carries_fresh_signed_ts(env):
    """The publish body ships a tz-aware ``ts`` INSIDE the signed canonical
    bytes, so the GFS can replay-guard it — only such a fresh publish may
    restore a listing the owner previously withdrew."""
    from socialhome.domain.space import (
        JoinMode,
        Space,
        SpaceFeatures,
        SpaceType,
    )
    from socialhome.repositories.space_repo import SqliteSpaceRepo

    db, conn_repo = env
    await conn_repo.save(_make_conn("gfs-ts", inbox_url="https://gfs.example"))
    space_repo = SqliteSpaceRepo(db)
    await space_repo.save(
        Space(
            id="sp-ts",
            name="Timestamped",
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
    session = _StubSession(method_responses={"POST": (200, {"status": "active"})})
    svc = GfsConnectionService(conn_repo, http_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    await svc.publish_space("sp-ts", "gfs-ts")

    body = session._last_body  # type: ignore[attr-defined]
    parsed = datetime.fromisoformat(body["ts"])
    assert parsed.tzinfo is not None, "ts must be tz-aware (a naive one is rejected)"
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 60
    # ``ts`` is covered by the signature, not bolted on beside it.
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
        self.gets: list[str] = []

    def get(self, url, **_kw):
        # ``/gfs/info`` with no ``anonymous_publish`` — i.e. a GFS predating
        # the anonymous relay, which is what these legacy-shape tests assert.
        self.gets.append(url)
        return _StubResp(200, {"server_name": "Legacy GFS"})

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _StubResp(self._status, {"status": "published"})


async def _publish_event_svc(env, session, *, space_id: str, gfs_ids: list[str]):
    """Service wired for publish_space_event, with the space published to
    each of *gfs_ids* (so ``list_gfs_for_space`` returns them)."""
    db, conn_repo = env
    for gid in gfs_ids:
        await conn_repo.save(
            _make_conn(
                gid,
                inbox_url=f"https://{gid}.example",
                # The GFS identity key this household pinned at pair time —
                # the only key a capability block may be verified against.
                public_key=_GFS_KP.public_key.hex(),
            )
        )
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


# ─── unsubscribe_from_gfs_space ──────────────────────────────────────────


async def test_unsubscribe_from_gfs_space_signs_body(env):
    """``unsubscribe_from_gfs_space`` signs the canonical
    ``{action, instance_id, space_id, ts}`` body with the household identity
    key and POSTs it to ``/gfs/subscribe``. The GFS requires the signature
    (an unsigned unsubscribe is a 403) and the ``action`` rides inside the
    signed bytes, so the signature can't be replayed as a subscribe."""
    _, repo = env
    await repo.save(_make_conn("gfs-unsub", inbox_url="https://gfs.example"))
    session = _StubSession(method_responses={"POST": (200, {"status": "removed"})})
    svc, pubkey = _signing_svc(repo, session)
    status = await svc.unsubscribe_from_gfs_space("sp-leave", "gfs-unsub")
    assert status == "removed"
    assert session.calls == [("POST", "https://gfs.example/gfs/subscribe")]
    body = session._last_body  # type: ignore[attr-defined]
    assert body["action"] == "unsubscribe"
    assert body["instance_id"] == "alpha.home"
    assert body["space_id"] == "sp-leave"
    assert body["ts"]
    canonical = json.dumps(
        {
            "action": "unsubscribe",
            "instance_id": "alpha.home",
            "space_id": "sp-leave",
            "ts": body["ts"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert verify_ed25519(pubkey, canonical, b64url_decode(body["signature"]))


async def test_unsubscribe_from_gfs_space_404_is_success(env):
    """Idempotent: already-absent on the GFS is not an error."""
    _, repo = env
    await repo.save(_make_conn("gfs-unsub", inbox_url="https://gfs.example"))
    session = _StubSession(method_responses={"POST": (404, {})})
    svc, _pk = _signing_svc(repo, session)
    assert await svc.unsubscribe_from_gfs_space("sp-gone", "gfs-unsub")


async def test_unsubscribe_from_gfs_space_raises_on_server_error(env):
    _, repo = env
    await repo.save(_make_conn("gfs-unsub", inbox_url="https://gfs.example"))
    session = _StubSession(method_responses={"POST": (500, {})})
    svc, _pk = _signing_svc(repo, session)
    with pytest.raises(GfsConnectionError):
        await svc.unsubscribe_from_gfs_space("sp-x", "gfs-unsub")


async def test_unsubscribe_from_gfs_space_unknown_gfs_raises(env):
    _, repo = env
    session = _StubSession(status=200)
    svc, _pk = _signing_svc(repo, session)
    with pytest.raises(GfsConnectionError):
        await svc.unsubscribe_from_gfs_space("sp-x", "nope")
    assert session.calls == []


async def test_unsubscribe_from_gfs_space_without_signing_key_raises(env):
    """Fail-closed: no signing identity → never send an unsigned body."""
    _, repo = env
    await repo.save(_make_conn("gfs-unsub", inbox_url="https://gfs.example"))
    session = _StubSession(status=200)
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsConnectionError):
        await svc.unsubscribe_from_gfs_space("sp-x", "gfs-unsub")
    assert session.calls == []


# ─── remote-authored error text (FIX 6) ──────────────────────────────────


async def test_remote_error_detail_is_truncated(caplog):
    """``GfsConnectionError`` text reaches the SPA as the 502
    ``GFS_UNAVAILABLE`` message (``routes/base.py``), so a hostile GFS must
    not be able to author arbitrarily long copy in the household's own error
    toast. The full body goes to the log instead."""
    resp = _StubResp(500, text="X" * 5000)
    with caplog.at_level(logging.WARNING):
        detail = await _remote_detail(resp, context="publish")

    assert len(detail) <= MAX_REMOTE_DETAIL_CHARS + len("… (truncated)")
    assert detail.endswith("… (truncated)")
    assert "X" * 5000 in caplog.text


async def test_short_remote_error_detail_passes_through():
    resp = _StubResp(400, text="space not published")
    assert await _remote_detail(resp, context="publish") == "space not published"


async def test_remote_error_detail_bounds_the_read():
    """Even the logged body is bounded — a multi-gigabyte error body must
    never be buffered whole."""
    resp = _StubResp(500, text="Y" * (1024 * 1024))
    detail = await _remote_detail(resp, context="subscribe")
    assert len(detail) <= MAX_REMOTE_DETAIL_CHARS + len("… (truncated)")


# ─── Anonymous publish (the GFS must not learn WHICH household relayed) ───


def _signed_info(
    *,
    gfs_instance_id: str = "inst-g1",
    capabilities: dict | None = None,
    kp=None,
    server_name: str = "GFS g1",
    suite: str = CAPS_SIG_SUITE_ED25519,
) -> dict:
    """The ``/gfs/info`` body a CURRENT connection server serves.

    The capability map travels with a signature made by the GFS identity key
    the household pinned at pair time — that signature is the ONLY thing the
    household may act on. The top-level ``anonymous_publish`` mirror stays for
    readability and is deliberately ignored by the client.
    """
    caps = {"anonymous_publish": True} if capabilities is None else capabilities
    sig, _real_suite = sign_capabilities(
        (kp or _GFS_KP).private_key, gfs_instance_id, caps
    )
    return {
        "server_name": server_name,
        "anonymous_publish": bool(caps.get("anonymous_publish")),
        "capabilities": caps,
        "capabilities_sig": sig,
        "capabilities_sig_suite": suite,
    }


def _stripped_info(server_name: str = "GFS g1") -> dict:
    """What an on-path attacker leaves behind: the unauthenticated flag still
    says ``true`` but the signed block is gone. Indistinguishable on the wire
    from an older GFS — and treated the same way (legacy body + a warning)."""
    return {"server_name": server_name, "anonymous_publish": True}


class _AnonSession:
    """aiohttp-session stub with a scriptable ``GET /gfs/info``.

    ``info`` is the body ``/gfs/info`` returns; ``raise_on_get`` makes the GET
    raise a transport error (the "GFS unreachable at publish time" case).
    Every GET and POST is recorded so a test can assert the info endpoint was
    hit exactly once across N publishes.
    """

    def __init__(
        self,
        *,
        info: dict | None = None,
        info_status: int = 200,
        raise_on_get: bool = False,
        status: int = 200,
    ) -> None:
        self.info = info if info is not None else {}
        self.info_status = info_status
        self.raise_on_get = raise_on_get
        self._status = status
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    def get(self, url, **_kw):
        self.gets.append(url)
        if self.raise_on_get:
            raise aiohttp.ClientError("boom")
        return _StubResp(self.info_status, self.info)

    def post(self, url, *, json=None, **_kw):
        self.posts.append((url, json or {}))
        return _StubResp(self._status, {"status": "published"})


def _freeze_clock(monkeypatch, clock: list[float]) -> None:
    """Drive the service's negative-TTL clock from *clock[0]*.

    Only the service module's own ``time`` binding is swapped — patching the
    real :func:`time.monotonic` would also freeze asyncio's event-loop clock
    and hang every timeout in the process.
    """
    monkeypatch.setattr(
        gfs_connection_service,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )


def test_the_service_does_not_import_the_gfs_server_package():
    """The household verifies capability blocks with
    :mod:`socialhome.capabilities_sig`, which lives at the package top level
    exactly so importing this service does NOT drag the GFS server package
    into every HFS process."""
    code = (
        "import sys; import socialhome.services.gfs_connection_service; "
        "print('socialhome.global_server' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "False", out.stdout


def _info_probes(session) -> int:
    return len([u for u in session.gets if u.endswith("/gfs/info")])


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "socialhome.services.gfs_connection_service"
    ]


async def test_publish_space_event_omits_identity_for_anonymous_gfs(env):
    """A GFS whose SIGNED capability block advertises ``anonymous_publish``
    gets the identity-free body: exactly ``{space_id, event_type, payload}`` —
    no ``from_instance``, no household transport ``signature``, no ``ts``, and
    this household's own instance id nowhere in the serialized bytes."""
    session = _AnonSession(info=_signed_info())
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-anon", gfs_ids=["g1"]
    )
    envelope = {"space_id": "sp-anon", "epoch": 0, "encrypted_payload": "ct"}
    delivered = await svc.publish_space_event(
        space_id="sp-anon",
        event_type="space_post_public",
        payload=envelope,
        from_instance="alpha.home",
    )
    assert delivered == 1
    _url, body = session.posts[0]
    assert set(body) == {"space_id", "event_type", "payload"}
    assert body["payload"] == envelope
    # The household identity must not survive anywhere in the wire bytes.
    assert "alpha.home" not in json.dumps(body)
    assert "from_instance" not in json.dumps(body)


async def test_publish_space_event_legacy_body_warns_once_per_connection(env, caplog):
    """A GFS that did NOT advertise the flag keeps the legacy signed body —
    and the privacy downgrade is logged as exactly ONE warning per connection
    per process, naming the connection (never the space)."""
    session = _AnonSession(info={"server_name": "Old GFS"})
    svc, kp = await _publish_event_svc(
        env, session, space_id="sp-legacy", gfs_ids=["g1"]
    )
    envelope = {"space_id": "sp-legacy", "epoch": 0}
    logger = "socialhome.services.gfs_connection_service"
    with caplog.at_level(logging.WARNING, logger=logger):
        for _ in range(3):
            assert (
                await svc.publish_space_event(
                    space_id="sp-legacy",
                    event_type="space_post_public",
                    payload=envelope,
                    from_instance="alpha.home",
                )
                == 1
            )
    # Byte-shape unchanged from before the anonymous-publish change.
    _url, body = session.posts[0]
    assert body["space_id"] == "sp-legacy"
    assert body["event_type"] == "space_post_public"
    assert body["payload"] == envelope
    assert body["from_instance"] == "alpha.home"
    signed = {k: v for k, v in body.items() if k != "signature"}
    canonical = json.dumps(signed, separators=(",", ":"), sort_keys=True).encode()
    assert verify_ed25519(kp.public_key, canonical, b64url_decode(body["signature"]))
    # One downgrade warning + one "no signed capability block" warning, each
    # emitted exactly once across the three publishes, naming the connection
    # and never the space.
    msgs = _warnings(caplog)
    assert len(msgs) == 2
    assert all("sp-legacy" not in m for m in msgs)
    assert all("g1" in m or "https://g1.example" in m for m in msgs)
    assert any("no signed capability block" in m for m in msgs)


async def test_stripped_capability_block_is_not_trusted(env, caplog):
    """THE downgrade attack: an on-path attacker strips the signed block from
    ``/gfs/info`` while leaving the bare ``anonymous_publish: true`` flag. The
    household must NOT act on the unauthenticated flag — trusting it would be
    a no-op for the attacker, who instead wants the opposite: forcing the
    legacy body (a household-signed, third-party-provable "household X relayed
    into space Y" artefact) is what stripping buys them. Either way the flag
    alone decides nothing; only the signature does."""
    session = _AnonSession(info=_stripped_info())
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-strip", gfs_ids=["g1"]
    )
    logger = "socialhome.services.gfs_connection_service"
    with caplog.at_level(logging.WARNING, logger=logger):
        await svc.publish_space_event(
            space_id="sp-strip",
            event_type="space_post_public",
            payload={"space_id": "sp-strip"},
            from_instance="alpha.home",
        )
    _url, body = session.posts[0]
    assert body["from_instance"] == "alpha.home"
    assert any("no signed capability block" in m for m in _warnings(caplog))


async def test_capability_block_signed_by_the_wrong_key_is_rejected(env, caplog):
    """A block signed by ANY key but the pinned one is tampering, and the
    warning says so — an operator has to be able to tell "old GFS" (no block)
    from "someone is rewriting my /gfs/info" (a block that fails)."""
    attacker = generate_identity_keypair()
    session = _AnonSession(info=_signed_info(kp=attacker))
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-evil", gfs_ids=["g1"]
    )
    logger = "socialhome.services.gfs_connection_service"
    with caplog.at_level(logging.WARNING, logger=logger):
        await svc.publish_space_event(
            space_id="sp-evil",
            event_type="space_post_public",
            payload={"space_id": "sp-evil"},
            from_instance="alpha.home",
        )
    assert session.posts[0][1]["from_instance"] == "alpha.home"
    msgs = _warnings(caplog)
    assert any("FAILED verification" in m for m in msgs)
    assert not any("no signed capability block" in m for m in msgs)


async def test_capability_block_bound_to_another_gfs_is_rejected(env, caplog):
    """The signature covers the GFS instance id, so a block lifted verbatim
    from another (legitimately signed) server does not authenticate this one —
    even when the attacker owns that other server's key."""
    session = _AnonSession(info=_signed_info(gfs_instance_id="inst-somewhere-else"))
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-lift", gfs_ids=["g1"]
    )
    logger = "socialhome.services.gfs_connection_service"
    with caplog.at_level(logging.WARNING, logger=logger):
        await svc.publish_space_event(
            space_id="sp-lift",
            event_type="space_post_public",
            payload={"space_id": "sp-lift"},
            from_instance="alpha.home",
        )
    assert session.posts[0][1]["from_instance"] == "alpha.home"
    assert any("FAILED verification" in m for m in _warnings(caplog))


async def test_unknown_capability_suite_is_unverifiable_not_fatal(env, caplog):
    """A suite this build doesn't know is rejected (no default fallback) — but
    it must never raise out of a best-effort fetch: the publish still goes out
    on the legacy path and the operator gets a warning naming the suite."""
    session = _AnonSession(info=_signed_info(suite="ed25519+mldsa65"))
    svc, _kp = await _publish_event_svc(env, session, space_id="sp-pq", gfs_ids=["g1"])
    logger = "socialhome.services.gfs_connection_service"
    with caplog.at_level(logging.WARNING, logger=logger):
        delivered = await svc.publish_space_event(
            space_id="sp-pq",
            event_type="space_post_public",
            payload={"space_id": "sp-pq"},
            from_instance="alpha.home",
        )
    assert delivered == 1
    assert session.posts[0][1]["from_instance"] == "alpha.home"
    assert any("ed25519+mldsa65" in m for m in _warnings(caplog))


async def test_signed_block_denying_the_capability_is_not_tamper_flagged(env, caplog):
    """A VALIDLY signed ``anonymous_publish: false`` is a GFS honestly saying
    it can't do the anonymous relay. Legacy body, yes — but no tampering /
    missing-block warning, or the honest answer would look like an attack."""
    session = _AnonSession(info=_signed_info(capabilities={"anonymous_publish": False}))
    svc, _kp = await _publish_event_svc(env, session, space_id="sp-no", gfs_ids=["g1"])
    logger = "socialhome.services.gfs_connection_service"
    with caplog.at_level(logging.WARNING, logger=logger):
        await svc.publish_space_event(
            space_id="sp-no",
            event_type="space_post_public",
            payload={"space_id": "sp-no"},
            from_instance="alpha.home",
        )
    assert session.posts[0][1]["from_instance"] == "alpha.home"
    msgs = _warnings(caplog)
    assert not any("FAILED verification" in m for m in msgs)
    assert not any("no signed capability block" in m for m in msgs)


async def test_verified_capability_cannot_be_downgraded_in_process(env, caplog):
    """In-process ratchet: once a connection has been seen advertising
    ``anonymous_publish`` under a VALID signature, a later fetch that lacks it
    does NOT flip the cache back. A GFS cannot legitimately lose the
    capability, so a mid-life downgrade is an attack (or a broken proxy) — the
    household keeps relaying identity-free and logs the attempt."""
    session = _AnonSession(info=_signed_info())
    svc, _kp = await _publish_event_svc(env, session, space_id="sp-rat", gfs_ids=["g1"])
    await svc.publish_space_event(
        space_id="sp-rat",
        event_type="space_post_public",
        payload={"space_id": "sp-rat"},
        from_instance="alpha.home",
    )
    assert set(session.posts[0][1]) == {"space_id", "event_type", "payload"}
    # The block disappears (attacker on-path, or a proxy eating the field).
    session.info = _stripped_info()
    logger = "socialhome.services.gfs_connection_service"
    with caplog.at_level(logging.WARNING, logger=logger):
        await svc.refresh_connection_metadata("g1")
        await svc.publish_space_event(
            space_id="sp-rat",
            event_type="space_post_public",
            payload={"space_id": "sp-rat"},
            from_instance="alpha.home",
        )
    assert set(session.posts[1][1]) == {"space_id", "event_type", "payload"}
    assert any("downgrade ignored" in m for m in _warnings(caplog))


async def test_the_ratchet_does_not_survive_a_restart(env):
    """The ratchet is RAM-only: a fresh process starts from "unknown" and has
    to learn the capability again from a signed block. (Persisting it would
    pin a remote server's build state in this household's database.)"""
    session = _AnonSession(info=_signed_info())
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-boot", gfs_ids=["g1"]
    )
    await svc.publish_space_event(
        space_id="sp-boot",
        event_type="space_post_public",
        payload={"space_id": "sp-boot"},
        from_instance="alpha.home",
    )
    assert set(session.posts[0][1]) == {"space_id", "event_type", "payload"}

    _db, conn_repo = env
    session.info = _stripped_info()
    restarted = GfsConnectionService(conn_repo, http_client=session)
    restarted.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=generate_identity_keypair().private_key,
    )
    await restarted.publish_space_event(
        space_id="sp-boot",
        event_type="space_post_public",
        payload={"space_id": "sp-boot"},
        from_instance="alpha.home",
    )
    assert session.posts[1][1]["from_instance"] == "alpha.home"


async def test_publish_space_event_fetches_info_once_on_cold_cache(env):
    """The flag is unknown on the first publish after boot → /gfs/info is
    fetched ONCE on demand and the answer cached for later publishes."""
    session = _AnonSession(info=_signed_info())
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-cold", gfs_ids=["g1"]
    )
    for _ in range(3):
        await svc.publish_space_event(
            space_id="sp-cold",
            event_type="space_post_public",
            payload={"space_id": "sp-cold"},
            from_instance="alpha.home",
        )
    assert _info_probes(session) == 1
    assert all(
        set(b) == {"space_id", "event_type", "payload"} for _u, b in session.posts
    )


async def test_publish_space_event_info_failure_falls_back_and_retries(
    env, monkeypatch
):
    """An unreachable /gfs/info downgrades THIS publish to the legacy body
    (which both an old and a new GFS accept). The failure is NOT cached as a
    capability — only as a short retry suppression — so once the TTL passes
    the next publish probes again and upgrades."""
    clock = [1000.0]
    _freeze_clock(monkeypatch, clock)
    session = _AnonSession(raise_on_get=True)
    svc, _kp = await _publish_event_svc(
        env, session, space_id="sp-retry", gfs_ids=["g1"]
    )
    await svc.publish_space_event(
        space_id="sp-retry",
        event_type="space_post_public",
        payload={"space_id": "sp-retry"},
        from_instance="alpha.home",
    )
    assert session.posts[0][1]["from_instance"] == "alpha.home"
    # The GFS comes back; after the negative TTL the probe is retried.
    session.raise_on_get = False
    session.info = _signed_info()
    clock[0] += GFS_INFO_NEGATIVE_TTL_S + 0.1
    await svc.publish_space_event(
        space_id="sp-retry",
        event_type="space_post_public",
        payload={"space_id": "sp-retry"},
        from_instance="alpha.home",
    )
    assert _info_probes(session) == 2
    assert set(session.posts[1][1]) == {"space_id", "event_type", "payload"}


async def test_failed_info_probe_is_negative_cached_for_the_ttl(env, monkeypatch):
    """A GFS whose ``/gfs/info`` is down but whose ``/gfs/publish`` is up used
    to cost a full 10 s connect timeout on EVERY publish (the answer is
    deliberately never cached as ``False``). A short negative TTL keeps the
    privacy property — the household still re-probes and upgrades — while
    collapsing a burst of publishes onto one probe."""
    clock = [500.0]
    _freeze_clock(monkeypatch, clock)
    session = _AnonSession(raise_on_get=True)
    svc, _kp = await _publish_event_svc(env, session, space_id="sp-ttl", gfs_ids=["g1"])

    async def _publish() -> None:
        await svc.publish_space_event(
            space_id="sp-ttl",
            event_type="space_post_public",
            payload={"space_id": "sp-ttl"},
            from_instance="alpha.home",
        )

    await _publish()
    clock[0] += GFS_INFO_NEGATIVE_TTL_S - 0.1
    await _publish()
    await _publish()
    assert _info_probes(session) == 1
    # Past the TTL the household probes again (no permanent downgrade).
    clock[0] += 0.2
    await _publish()
    assert _info_probes(session) == 2
    assert all("from_instance" in b for _u, b in session.posts)


async def test_ws_reconnect_refresh_upgrades_the_publish_body(env):
    """``refresh_connection_metadata`` (run on every GFS-WS reconnect) is what
    keeps the cached flag fresh: an old GFS that gets upgraded starts receiving
    identity-free bodies on the next publish, with no extra probe."""
    session = _AnonSession(info={"server_name": "GFS g1"})
    svc, _kp = await _publish_event_svc(env, session, space_id="sp-up", gfs_ids=["g1"])
    await svc.refresh_connection_metadata("g1")
    await svc.publish_space_event(
        space_id="sp-up",
        event_type="space_post_public",
        payload={"space_id": "sp-up"},
        from_instance="alpha.home",
    )
    assert "from_instance" in session.posts[0][1]
    # Operator upgrades the GFS; the next reconnect refresh learns the flag.
    session.info = _signed_info()
    await svc.refresh_connection_metadata("g1")
    await svc.publish_space_event(
        space_id="sp-up",
        event_type="space_post_public",
        payload={"space_id": "sp-up"},
        from_instance="alpha.home",
    )
    assert set(session.posts[1][1]) == {"space_id", "event_type", "payload"}
    # Only the two reconnect refreshes hit /gfs/info — no on-demand probe.
    assert _info_probes(session) == 2


async def test_pair_seeds_the_capability_only_from_a_verified_block(env):
    """At pair time the household verifies the block against the ``public_key``
    in the SAME response — the one it is about to pin (TOFU). A verified block
    seeds the cache, so the first relay to a freshly-paired GFS is already
    identity-free without a second round-trip."""
    _db, repo = env
    gfs_kp = generate_identity_keypair()
    info = {
        "gfs_instance_id": "fresh-gfs",
        "public_key": gfs_kp.public_key.hex(),
        "server_name": "Fresh GFS",
        **_signed_info(gfs_instance_id="fresh-gfs", kp=gfs_kp),
    }
    session = _StubSession(
        method_responses={
            "GET": (200, info),
            "POST": (200, {"status": "registered"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session)
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=generate_identity_keypair().private_key,
    )
    conn = await svc.pair(
        {"gfs_url": "https://fresh.example", "token": "tok"}, **_OWN_PAIR_KW
    )
    await repo.publish_space("sp-fresh", conn.id)
    await svc.publish_space_event(
        space_id="sp-fresh",
        event_type="space_post_public",
        payload={"space_id": "sp-fresh"},
        from_instance="alpha.home",
    )
    # No extra /gfs/info probe, and the very first relay is identity-free.
    assert [m for m, _u in session.calls].count("GET") == 1
    assert set(session._last_body or {}) == {"space_id", "event_type", "payload"}


async def test_pair_does_not_seed_the_capability_from_an_unsigned_flag(env):
    """Pairing over a stripped (or simply older) ``/gfs/info`` seeds the cache
    with ``False`` — the bare flag proves nothing, so the first relay carries
    the identified legacy body until a signed block shows up."""
    _db, repo = env
    gfs_kp = generate_identity_keypair()
    session = _StubSession(
        method_responses={
            "GET": (
                200,
                {
                    "gfs_instance_id": "fresh-gfs",
                    "public_key": gfs_kp.public_key.hex(),
                    "server_name": "Fresh GFS",
                    "anonymous_publish": True,
                },
            ),
            "POST": (200, {"status": "registered"}),
        },
    )
    svc = GfsConnectionService(repo, http_client=session)
    svc.attach_publish_context(
        space_repo=None,
        own_instance_id="alpha.home",
        own_signing_key=generate_identity_keypair().private_key,
    )
    conn = await svc.pair(
        {"gfs_url": "https://fresh.example", "token": "tok"}, **_OWN_PAIR_KW
    )
    await repo.publish_space("sp-fresh", conn.id)
    await svc.publish_space_event(
        space_id="sp-fresh",
        event_type="space_post_public",
        payload={"space_id": "sp-fresh"},
        from_instance="alpha.home",
    )
    assert (session._last_body or {})["from_instance"] == "alpha.home"


# ─── heal_space_pins (self-heal a NULL authority pin on WS connect) ────────


class _FakeSpaceRepo:
    """Minimal space repo for the pin self-heal: a space table + seeds.

    ``kek=False`` reproduces a household with no key manager wired, where
    ``get_space_seed`` raises ``RuntimeError`` — the heal must skip, not blow
    up the connect hook.
    """

    def __init__(self, spaces: dict, seeds: dict, *, kek: bool = True) -> None:
        self._spaces = spaces
        self._seeds = seeds
        self._kek = kek

    async def get(self, space_id: str):
        return self._spaces.get(space_id)

    async def get_space_seed(self, space_id: str) -> bytes | None:
        if not self._kek:
            raise RuntimeError("space seed access requires a key_manager")
        return self._seeds.get(space_id)


def _fake_space(space_id: str):
    from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType

    return Space(
        id=space_id,
        name=f"Space {space_id}",
        owner_instance_id="alpha.home",
        owner_username="alice",
        identity_public_key="aa" * 32,
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
    )


async def _heal_svc(env, session, *, gfs_id: str, spaces: list[str], space_repo):
    _db, conn_repo = env
    await conn_repo.save(_make_conn(gfs_id, inbox_url=f"https://{gfs_id}.example"))
    for sid in spaces:
        await conn_repo.publish_space(sid, gfs_id)
    kp = generate_identity_keypair()
    svc = GfsConnectionService(conn_repo, http_client=session)
    svc.attach_publish_context(
        space_repo=space_repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    return svc


async def test_heal_space_pins_republishes_each_published_space(env):
    """Every space this household published to the reconnecting GFS is
    re-published once, so a GFS row with a NULL authority pin heals."""
    session = _AnonSession()
    repo = _FakeSpaceRepo(
        {"sp-a": _fake_space("sp-a"), "sp-b": _fake_space("sp-b")},
        {"sp-a": b"\x01" * 32, "sp-b": b"\x02" * 32},
    )
    svc = await _heal_svc(
        env, session, gfs_id="g1", spaces=["sp-a", "sp-b"], space_repo=repo
    )
    healed = await svc.heal_space_pins("g1")
    assert healed == 2
    urls = sorted(u for u, _b in session.posts)
    assert urls == [
        "https://g1.example/gfs/spaces/sp-a/publish",
        "https://g1.example/gfs/spaces/sp-b/publish",
    ]
    assert all("identity_public_key" in b for _u, b in session.posts)


async def test_heal_space_pins_skips_seedless_and_missing_spaces(env):
    """A space this household holds no seed for (pure subscriber) and one whose
    local row is gone are both skipped — only the seed-held space republishes."""
    session = _AnonSession()
    repo = _FakeSpaceRepo(
        {"sp-a": _fake_space("sp-a"), "sp-noseed": _fake_space("sp-noseed")},
        {"sp-a": b"\x01" * 32},
    )
    svc = await _heal_svc(
        env,
        session,
        gfs_id="g1",
        spaces=["sp-a", "sp-noseed", "sp-gone"],
        space_repo=repo,
    )
    assert await svc.heal_space_pins("g1") == 1
    assert [u for u, _b in session.posts] == [
        "https://g1.example/gfs/spaces/sp-a/publish"
    ]


async def test_heal_space_pins_skips_everything_without_a_kek(env):
    """No household key manager wired → ``get_space_seed`` raises; the heal
    swallows it and publishes nothing rather than breaking the connect."""
    session = _AnonSession()
    repo = _FakeSpaceRepo({"sp-a": _fake_space("sp-a")}, {}, kek=False)
    svc = await _heal_svc(env, session, gfs_id="g1", spaces=["sp-a"], space_repo=repo)
    assert await svc.heal_space_pins("g1") == 0
    assert session.posts == []


async def test_heal_space_pins_never_raises_on_a_failing_gfs(env):
    """A GFS rejecting the re-publish is logged and skipped — the connect hook
    must never see an exception."""
    session = _AnonSession(status=503)
    repo = _FakeSpaceRepo({"sp-a": _fake_space("sp-a")}, {"sp-a": b"\x01" * 32})
    svc = await _heal_svc(env, session, gfs_id="g1", spaces=["sp-a"], space_repo=repo)
    assert await svc.heal_space_pins("g1") == 0


async def test_heal_space_pins_noops_for_unknown_or_inactive_gfs(env):
    """An unknown or non-active connection heals nothing and sends nothing."""
    session = _AnonSession()
    _db, conn_repo = env
    await conn_repo.save(_make_conn("g-sus", status="suspended"))
    repo = _FakeSpaceRepo({"sp-a": _fake_space("sp-a")}, {"sp-a": b"\x01" * 32})
    kp = generate_identity_keypair()
    svc = GfsConnectionService(conn_repo, http_client=session)
    svc.attach_publish_context(
        space_repo=repo,
        own_instance_id="alpha.home",
        own_signing_key=kp.private_key,
    )
    assert await svc.heal_space_pins("nope") == 0
    assert await svc.heal_space_pins("g-sus") == 0
    assert session.posts == []


async def test_heal_space_pins_noops_without_publish_context(env):
    """No space repo wired → nothing to publish, no network."""
    session = _AnonSession()
    _db, conn_repo = env
    await conn_repo.save(_make_conn("g1"))
    await conn_repo.publish_space("sp-a", "g1")
    svc = GfsConnectionService(conn_repo, http_client=session)
    assert await svc.heal_space_pins("g1") == 0
    assert session.posts == []


# ─── End-to-end against a REAL GFS (both-ways compatibility) ──────────────
#
# These boot an actual ``create_gfs_app`` behind an aiohttp ``TestServer``
# plus a recording "subscriber inbox" server, and drive the real household
# service against them: a current household + current GFS relay an
# identity-free frame, and the LEGACY body this same sender still emits for a
# GFS that never advertised ``anonymous_publish`` is accepted by the new GFS
# too — so the fallback can never strand a household.

_E2E_OWN_INSTANCE = "alpha.home"
_E2E_SUB_INSTANCE = "sub.home"
_E2E_SPACE_ID = "sp-e2e"


class _SeedSpaceRepo:
    """Space repo stand-in holding one publishable space + its authority seed."""

    def __init__(self, space, seed: bytes) -> None:
        self._space = space
        self._seed = seed

    async def get(self, space_id: str):
        return self._space if space_id == self._space.id else None

    async def get_space_seed(self, space_id: str) -> bytes | None:
        return self._seed if space_id == self._space.id else None


@pytest.fixture
async def inbox_sink():
    """A tiny HTTPS inbox recording the relay frames the GFS fans out."""
    received: list[dict] = []

    async def _handle(request: web.Request) -> web.Response:
        received.append(await request.json())
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.router.add_post("/inbox", _handle)
    async with TestClient(TestServer(app)) as tc:
        yield tc, received


@pytest.fixture
async def real_gfs(tmp_path):
    app = create_gfs_app(db_path=tmp_path / "gfs-e2e.db")
    async with TestClient(TestServer(app)) as tc:
        yield tc


@pytest.fixture
async def e2e_sender(tmp_dir, real_gfs, inbox_sink):
    """A real :class:`GfsConnectionService` wired against the real GFS.

    Registers the household + one subscriber on the GFS, publishes the space
    metadata through the REAL publish path (TOFU-pinning the space's authority
    key), and subscribes the sink so a relay actually fans out. Yields
    ``(svc, space_seed, received_frames)``.
    """
    from socialhome.domain.space import JoinMode, Space, SpaceFeatures, SpaceType

    sink_client, received = inbox_sink
    fed_repo = real_gfs.server.app[gfs_fed_repo_key]

    household_kp = generate_identity_keypair()
    space_kp = generate_identity_keypair()

    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id=_E2E_OWN_INSTANCE,
            display_name="Alpha",
            public_key=household_kp.public_key.hex(),
            inbox_url="https://alpha.example/federation/inbox/x",
            status="active",
            auto_accept=True,
        )
    )
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id=_E2E_SUB_INSTANCE,
            display_name="Sub",
            public_key=generate_identity_keypair().public_key.hex(),
            inbox_url=str(sink_client.make_url("/inbox")),
            status="active",
            auto_accept=True,
        )
    )

    db = AsyncDatabase(tmp_dir / "hfs-e2e.db", batch_timeout_ms=10)
    await db.startup()
    conn_repo = SqliteGfsConnectionRepo(db)
    gfs_base = str(real_gfs.make_url("")).rstrip("/")
    await conn_repo.save(
        GfsConnection(
            id="gfs-e2e",
            gfs_instance_id="gfs-inst",
            display_name="E2E GFS",
            public_key="ab" * 32,
            inbox_url=gfs_base,
            status="active",
            paired_at="2026-01-01T00:00:00+00:00",
        )
    )

    space = Space(
        id=_E2E_SPACE_ID,
        name="E2E Space",
        owner_instance_id=_E2E_OWN_INSTANCE,
        owner_username="alice",
        identity_public_key=space_kp.public_key.hex(),
        config_sequence=0,
        features=SpaceFeatures(),
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.OPEN,
    )
    async with aiohttp.ClientSession() as session:
        svc = GfsConnectionService(conn_repo, http_client=session)
        svc.attach_publish_context(
            space_repo=_SeedSpaceRepo(space, space_kp.private_key),
            own_instance_id=_E2E_OWN_INSTANCE,
            own_signing_key=household_kp.private_key,
        )
        # Real metadata publish → the GFS TOFU-pins the space authority key,
        # which is the ONLY thing authorizing the anonymous relay below.
        await svc.publish_space(_E2E_SPACE_ID, "gfs-e2e")
        await fed_repo.add_subscriber(
            space_id=_E2E_SPACE_ID, instance_id=_E2E_SUB_INSTANCE
        )
        yield svc, space_kp.private_key, received
    await db.shutdown()


def _authority_payload(seed: bytes, *, post_id: str) -> dict:
    payload = {
        "space_id": _E2E_SPACE_ID,
        "epoch": 0,
        "post_id": post_id,
        "encrypted_payload": "Y2lwaGVy",
    }
    payload.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id=_E2E_SPACE_ID,
            payload=payload,
            space_seed=seed,
        )
    )
    return payload


async def test_e2e_new_household_relays_identity_free_through_a_real_gfs(e2e_sender):
    """Current sender + current GFS: the publish carries no household identity,
    the GFS accepts it on the space-authority signature alone, and the frame it
    fans out to subscribers is identity-free too."""
    svc, seed, received = e2e_sender
    delivered = await svc.publish_space_event(
        space_id=_E2E_SPACE_ID,
        event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        payload=_authority_payload(seed, post_id="p-anon"),
        from_instance=_E2E_OWN_INSTANCE,
    )
    assert delivered == 1
    assert len(received) == 1
    frame = received[0]
    assert set(frame) == {"space_id", "event_type", "payload"}
    assert frame["payload"]["post_id"] == "p-anon"
    assert _E2E_OWN_INSTANCE not in json.dumps(frame)


async def test_e2e_legacy_body_from_this_sender_still_accepted_by_a_new_gfs(
    e2e_sender,
):
    """Compat the other way: with the capability cached as 'legacy' (an older
    GFS, or an unreachable /gfs/info) this sender emits the identified body —
    and a NEW GFS still accepts it, verifying then discarding the household
    identity, so the safe default can never strand a household."""
    svc, seed, received = e2e_sender
    svc._anon_publish["gfs-e2e"] = False  # noqa: SLF001 — pin the capability
    delivered = await svc.publish_space_event(
        space_id=_E2E_SPACE_ID,
        event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        payload=_authority_payload(seed, post_id="p-legacy"),
        from_instance=_E2E_OWN_INSTANCE,
    )
    assert delivered == 1
    # Accepted — and the GFS still strips the identity out of the fan-out.
    assert len(received) == 1
    assert set(received[0]) == {"space_id", "event_type", "payload"}
    assert _E2E_OWN_INSTANCE not in json.dumps(received[0])


async def test_e2e_pair_learns_anonymous_publish_from_a_real_signed_block(
    tmp_dir, real_gfs
):
    """Pairing against a REAL GFS learns ``anonymous_publish`` through the
    signed capability block, verified against the key pinned in that same
    response — and a later ``/gfs/info`` refresh keeps it.

    This is the only test that exercises ``sign_capabilities`` (GFS side) and
    ``verify_capabilities`` (household side) against each other over the wire,
    so it is what catches canonicalisation drift between the two packages.
    """
    token, _wait = await real_gfs.server.app["gfs_token_service"].generate("127.0.0.55")
    assert token is not None
    gfs_base = str(real_gfs.make_url("")).rstrip("/")

    db = AsyncDatabase(tmp_dir / "hfs-pair-e2e.db", batch_timeout_ms=10)
    await db.startup()
    try:
        conn_repo = SqliteGfsConnectionRepo(db)
        async with aiohttp.ClientSession() as session:
            svc = GfsConnectionService(conn_repo, http_client=session)
            conn = await svc.pair(
                {"gfs_url": gfs_base, "token": token},
                own_instance_id=_E2E_OWN_INSTANCE,
                own_public_key_hex=generate_identity_keypair().public_key.hex(),
                own_inbox_url="https://alpha.example/federation/inbox",
                own_display_name="Alpha House",
            )
            # Learned from the signed block, not the bare flag: the pinned key
            # verified the signature over {gfs_instance_id, capabilities}.
            assert svc._anon_publish[conn.id] is True  # noqa: SLF001
            # A refresh re-verifies the same block and must not downgrade.
            await svc.refresh_connection_metadata(conn.id)
            assert svc._anon_publish[conn.id] is True  # noqa: SLF001
    finally:
        await db.shutdown()


# ── envelope_relay capability (§D2b invite bootstrap) ──────────────────────


async def test_envelope_relay_supported_reads_the_signed_block(env):
    """Only the SIGNED capability block grants the relay — the same rule
    ``anonymous_publish`` follows, for the same reason."""
    _db, repo = env
    conn = _make_conn("er-1", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _AnonSession(
        info=_signed_info(
            gfs_instance_id=conn.gfs_instance_id,
            capabilities={"anonymous_publish": True, "envelope_relay": True},
        ),
    )
    svc = GfsConnectionService(repo, http_client=session)
    assert await svc.envelope_relay_supported(conn) is True
    # Cached after the first probe — a redeem must not re-fetch /gfs/info.
    assert await svc.envelope_relay_supported(conn) is True
    assert len(session.gets) == 1


async def test_envelope_relay_absent_from_the_block_is_false(env):
    _db, repo = env
    conn = _make_conn("er-2", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _AnonSession(
        info=_signed_info(
            gfs_instance_id=conn.gfs_instance_id,
            capabilities={"anonymous_publish": True},
        ),
    )
    svc = GfsConnectionService(repo, http_client=session)
    assert await svc.envelope_relay_supported(conn) is False


async def test_envelope_relay_from_a_stripped_block_is_false(env):
    """An unsigned flag is an on-path attacker's, not a capability."""
    _db, repo = env
    conn = _make_conn("er-3", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _AnonSession(info={"server_name": "x", "envelope_relay": True})
    svc = GfsConnectionService(repo, http_client=session)
    assert await svc.envelope_relay_supported(conn) is False


async def test_envelope_relay_unreachable_gfs_is_false_and_suppressed(env):
    """An unreachable probe answers ``False`` for this attempt and is not
    re-tried until the negative TTL lapses."""
    _db, repo = env
    conn = _make_conn("er-4", public_key=_GFS_KP.public_key.hex())
    await repo.save(conn)
    session = _AnonSession(raise_on_get=True)
    svc = GfsConnectionService(repo, http_client=session)
    assert await svc.envelope_relay_supported(conn) is False
    assert await svc.envelope_relay_supported(conn) is False
    assert len(session.gets) == 1


async def test_client_exposes_the_shared_session(env):
    """Sibling GFS-facing services borrow this session rather than opening
    a second connection pool."""
    _db, repo = env
    session = _AnonSession()
    svc = GfsConnectionService(repo, http_client=session)
    assert svc.client() is session
